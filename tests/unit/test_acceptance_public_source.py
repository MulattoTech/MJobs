"""No-network contracts for the same-profile public observation helper."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cli.acceptance_public_source import run, _digest
from services.discovery.sources.registry import source_adapter


STAMP = '2026-11-06T12:00:00+00:00'
VERSION = '1' * 64
BASELINE = dict(source={'commit': 'a' * 40}, profile_id='profile', application_id='app',
    operation_id='original-operation', request_id='original-request', external_submission_enabled=False,
    selected_application_job_id='fixture-job', selected_application_route_id='fixture-route')
PROOF = dict(scheduler_contained=True, source_policy_exclusive=True, source_collection_exclusive=True,
    owner='test-operator', token='test-lease', lease_expires_at='2026-11-06T12:05:00+00:00')


class ControlledAPI:
    def __init__(self):
        self.calls = []
        self.source = None
        self.state = 'COMPLETED'
        self.after_collection = lambda: None
        self.children = []
        self.children_complete = True
        self.child_polls = 0
        self.after_child_poll = lambda: None
        self.enrolled_sources = []
        self.domain_proofs = {}
        self.domain_version = 1
        self.domain_response_transform = lambda value: value
        self.profile = dict(id='profile', version=VERSION)
        self.app = dict(id='app', profile_id='profile', job_id='fixture-job', route_id='fixture-route',
            target='MOCK', state='CONFIRMED', confirmation={'receipt': 'mock-only'})
        self.fixture = dict(id='fixture-job', attributes={'fictional': True}, routes=[
            dict(id='fixture-route', job_id='fixture-job', provider='mock-ats', url='http://127.0.0.1:1/apply')])
        from packages.persistence.store import identity
        raw = dict(guid='https://weworkremotely.com/remote-jobs/provider-job',
            link='https://weworkremotely.com/remote-jobs/provider-job',
            title='Public Employer: Public Python role', description='Build Python services.')
        company_id = identity('wwr', 'public-employer')
        storage_id = identity(company_id, 'provider-job')
        checksum = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
        self.public = dict(id='real-job', company_id=company_id, company='Public Employer', title='Public Python role', attributes=dict(fictional=False, provider='wwr',
            source_id='9999999999999999999999999999999999999999999999999999999999999999', source_endpoint='https://weworkremotely.com/remote-jobs.rss',
            native_posting_id='provider-job', submission_capability='MANUAL_ONLY'),
            match=dict(profile_id='profile', profile_version=VERSION), postings=[dict(
                id=identity('9999999999999999999999999999999999999999999999999999999999999999', storage_id), job_id='real-job', source_job_id=storage_id, source='https://weworkremotely.com/remote-jobs.rss',
                fetched_at=STAMP, raw=raw, snapshots=[dict(id='snapshot', sha256=checksum, retrieved_at=STAMP)])])

    def configured(self, enabled=True, provider='wwr'):
        policy = dict(provider=provider, tenant='default', enabled=enabled, poll_interval=604800,
            concurrency=1, request_timeout=10, rate_limit=1, jitter_seconds=0)
        capabilities = source_adapter(policy).capabilities()
        self.source = dict(id='9999999999999999999999999999999999999999999999999999999999999999', **policy,
            strategy=capabilities.strategy.value, capabilities=capabilities.as_dict(), last_attempt=None,
            last_success=None, backoff_until=None, health_status='NOT_CHECKED', jobs_seen=0)
        return self.source

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if (method, path) in [('GET', '/settings'), ('GET', '/health')]:
            return dict(operation_mode='FIXTURE', external_submission_enabled=False)
        if (method, path) == ('GET', '/profiles/profile'):
            return deepcopy(self.profile)
        if (method, path) == ('GET', '/profiles/profile/job-inventory'):
            from packages.persistence.job_inventory import digest, snapshot_digest
            assert kwargs.get('params') == {'limit': 200}
            # Existing selected mock job is part of the original persisted catalogue.
            # Later public/native results must not be mistaken for original jobs.
            original = {
                'job_id': 'fixture-job', 'canonical_sha256': digest(self.fixture),
                'route_count': 1, 'source_copy_count': 0, 'source_copies': [],
                'source_copies_sha256': digest([]),
            }
            jobs = [original]
            return {
                'schema_version': 4, 'scope': 'ALL_PERSISTED_JOBS',
                'profile_id': 'profile', 'profile_version': self.profile['version'],
                'total': len(jobs), 'source_copy_count': 0,
                'snapshot_sha256': snapshot_digest('profile', self.profile['version'], jobs),
                'after': None, 'jobs': jobs, 'complete': True, 'next_cursor': None,
            }
        if (method, path) == ('GET', '/profiles/profile/applications/app'):
            return deepcopy(self.app)
        if (method, path) == ('GET', '/jobs/fixture-job'):
            return deepcopy(self.fixture)
        if (method, path) == ('GET', '/sources'):
            return {'sources': [deepcopy(self.source)] if self.source else []}
        if (method, path) == ('PUT', '/sources'):
            if self.source is None:
                self.configured(provider=kwargs['json']['provider'])
            self.source.update(kwargs['json'])
            return {'id': '9999999999999999999999999999999999999999999999999999999999999999'}
        if (method, path) == ('POST', '/sources/9999999999999999999999999999999999999999999999999999999999999999/collect'):
            self.params = dict(kwargs['json'], source_id='9999999999999999999999999999999999999999999999999999999999999999', profile_version=VERSION)
            self.source.update(last_attempt=STAMP, last_success=STAMP, jobs_seen=1, health_status='HEALTHY')
            self.after_collection()
            return {'operation_id': 'public-operation'}
        if (method, path) == ('GET', '/operations/public-operation'):
            return dict(id='public-operation', kind='DISCOVER', entity_id='profile', params=self.params,
                state=self.state, error='SOURCE_UNAVAILABLE' if self.state == 'BLOCKED' else None,
                attempt_count=1, retry_count=0, started_at=STAMP, completed_at=STAMP)
        if (method, path) == ('GET', '/profiles/profile/sources/9999999999999999999999999999999999999999999999999999999999999999/collections/public-operation/children'):
            self.child_polls += 1
            self.after_child_poll()
            value = dict(schema_version=1, profile_id='profile', profile_version=VERSION,
                source_id='9999999999999999999999999999999999999999999999999999999999999999', parent_operation_id='public-operation', parent_operation_attempt=1,
                parent_request_id=self.params['request_id'], complete=self.children_complete, limit=2,
                children=deepcopy(self.children), enrolled_sources=deepcopy(self.enrolled_sources))
            value['lineage_sha256'] = _digest([{key: val for key, val in row.items()
                if key not in {'state', 'authorization', 'reason', 'operation_attempt', 'operation_sha256'}}
                for row in sorted(value['children'], key=lambda item: item['request_id'])])
            value['sha256'] = _digest(value)
            self.last_children = deepcopy(value)
            return value
        if (method, path) == ('GET', '/profiles/profile/sources/9999999999999999999999999999999999999999999999999999999999999999/collections/public-operation/domain-proofs'):
            assert kwargs.get('params') == {'attempt': 1}
            assert self.source['enabled'] is True
            collection = deepcopy(self.last_children)
            value = {key: collection[key] for key in ('schema_version', 'profile_id', 'profile_version',
                'source_id', 'parent_operation_id', 'parent_operation_attempt', 'parent_request_id', 'complete', 'limit')}
            value.update(collection_lineage_sha256=collection['lineage_sha256'],
                collection_readback_sha256=collection['sha256'], observations=[])
            value['schema_version'] = self.domain_version
            for child in collection['children']:
                proof = deepcopy(self.domain_proofs.get(child['request_id']))
                enrolled = [{key: row[key] for key in ('source_id', 'company_source_id', 'operation_id',
                    'request_id', 'audit_id', 'audit_sha256', 'pending_sha256', 'receipt_sha256', 'envelope_sha256')}
                    for row in collection['enrolled_sources'] if row['request_id'] == child['request_id']]
                value['observations'].append(dict(child=child, status='AVAILABLE' if proof else 'UNAVAILABLE',
                    reason=None if proof else 'BOOTSTRAP_RECEIPT_NOT_PERSISTED', proof=proof, enrolled_sources=enrolled))
            value['sha256'] = _digest(value)
            return self.domain_response_transform(value)
        if (method, path) == ('POST', '/operations/public-operation/cancel'):
            return {'cancel_requested': True}
        if (method, path) == ('GET', '/jobs'):
            return {'jobs': [deepcopy(self.public)]}
        if (method, path) == ('GET', '/jobs/real-job'):
            return deepcopy(self.public)
        raise AssertionError((method, path, kwargs))


class PublicSourceObservationTests(unittest.TestCase):
    def setUp(self):
        self.api = ControlledAPI()
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / 'public.json'
        self.observation_count = 0

    def observe(self, **kwargs):
        self.observation_count += 1
        path = self.path if self.observation_count == 1 else self.path.with_name(
            'public-' + str(self.observation_count) + '.json')
        return run('http://127.0.0.1:18700', BASELINE, path,
            acceptance_run_id='campaign', request=self.api.request, execution_scope='CONTROLLED_TRANSPORT',
            containment_preflight=kwargs.pop('containment_preflight', lambda *_: deepcopy(PROOF)),
            utc_now=kwargs.pop('utc_now', lambda: datetime.fromisoformat(STAMP)), **kwargs)

    def test_safe_containment_codes_are_preserved_without_changing_failure_or_cleanup(self):
        from cli.acceptance_containment import ContainmentError
        for code in ('CHILD_READBACK_PARENT_STATE_INVALID', 'CHILD_OPERATION_CHANGED',
                     'CHILD_SNAPSHOT_NOT_STABLE', 'CONTAINMENT_LEASE_EXPIRED',
                     'CHILD_PARENT_IDENTITY_CHANGED', 'UNOWNED_COMPANY_DISCOVERY_EXISTS'):
            with self.subTest(code=code):
                self.api = ControlledAPI()
                stages = []

                def containment(stage, context):
                    stages.append(stage)
                    if stage == 'poll':
                        raise ContainmentError(code)
                    return deepcopy(PROOF)

                result = self.observe(containment_preflight=containment)
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'PUBLIC_OBSERVATION_API_OR_RECORD_INVALID')
                self.assertEqual(result['containment_failure'], dict(schema_version=1, stage='poll',
                    error_class='ContainmentError', code=code))
                self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
                self.assertEqual(stages.count('poll'), 1)
                self.assertNotIn('cleanup', stages)
                self.assertTrue(self.api.source['enabled'])
                self.assertEqual(result['acceptance_observations'][0]['status'], 'NOT_RUN')

    def test_safe_source_setup_codes_are_recorded_without_dispatch_or_secret_text(self):
        from cli.acceptance_source_setup import SourceSetupError
        for code in ('ADDITIONAL_SOURCE_ABSENCE_NOT_CONFIRMED',
                     'SOURCE_SETUP_LEASE_CONTINUITY_INVALID',
                     'SOURCE_SETUP_FIXTURE_MODE_REQUIRED'):
            with self.subTest(code=code):
                self.api = ControlledAPI()
                stages = []
                def containment(stage, context):
                    stages.append(stage)
                    if stage == 'preflight':
                        raise SourceSetupError(code)
                    return deepcopy(PROOF)
                result = self.observe(containment_preflight=containment)
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'PUBLIC_OBSERVATION_API_OR_RECORD_INVALID')
                self.assertEqual(result['containment_failure'], dict(schema_version=1,
                    stage='preflight', error_class='SourceSetupError', code=code))
                self.assertIsNone(self.api.source)
                self.assertEqual(stages, ['preflight'])
                self.assertFalse(any(method != 'GET' for method, _, _ in self.api.calls))
        for error in (SourceSetupError('Bearer synthetic-secret-never-serialize'),
                      SourceSetupError('UNKNOWN_CODE'),
                      SourceSetupError('SOURCE_SETUP_FIXTURE_MODE_REQUIRED', 'secret')):
            self.api = ControlledAPI()
            def containment(stage, context):
                raise error
            result = self.observe(containment_preflight=containment)
            self.assertNotIn('containment_failure', result)
            self.assertNotIn('synthetic-secret', json.dumps(result))

    def test_employer_capture_requires_saved_ui_witness_before_any_mutation(self):
        result = self.observe(employer_verification=True)
        self.assertEqual(result['result'], 'FAIL')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))
        self.assertIsNone(self.api.source)

    def test_opted_in_empty_employer_cohort_is_partial_and_never_activates_profile(self):
        from tests.unit.test_acceptance_employer_verification_opt_in import _controlled_profile, _controlled_ui_artifact
        self.api.profile = _controlled_profile(profile_id='profile', version=VERSION)
        baseline = {**BASELINE, **_controlled_ui_artifact(profile=self.api.profile,
            source=BASELINE['source'], run_id='campaign', origin='http://127.0.0.1:18700')}
        baseline['timestamp'] = STAMP
        baseline['employer_verification_opt_in'].update(saved_at_utc=STAMP, baseline_bound_at_utc=STAMP)
        def request(method, path, **kwargs):
            if path == '/company-sources/view':
                self.assertEqual(method, 'GET')
                return {'company_sources': []}
            if path.endswith('/inventory-proofs'):
                self.assertEqual(method, 'GET')
                collection = self.api.last_children
                value = {key: collection[key] for key in ('schema_version', 'profile_id', 'profile_version',
                    'source_id', 'parent_operation_id', 'parent_operation_attempt', 'parent_request_id', 'complete', 'limit')}
                value.update(collection_lineage_sha256=collection['lineage_sha256'],
                    collection_readback_sha256=collection['sha256'], observations=[])
                value['sha256'] = _digest(value)
                return value
            return self.api.request(method, path, **kwargs)
        before = deepcopy(self.api.profile)
        result = run('http://127.0.0.1:18700', baseline, self.path,
            acceptance_run_id='campaign', request=request, execution_scope='CONTROLLED_TRANSPORT',
            containment_preflight=lambda *_: deepcopy(PROOF), employer_verification=True,
            utc_now=lambda: datetime.fromisoformat(STAMP))
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['employer_observation']['status'], 'PARTIAL')
        self.assertEqual(result['employer_observation']['public_acceptance'], 'NOT_RUN')
        self.assertEqual(self.api.profile, before)
        self.assertFalse(any(method != 'GET' and path.startswith('/profiles/') for method, path, _ in self.api.calls))

        # Failure to read the original inventory is not an empty inventory.
        # It must stop before dispatch, even with an otherwise empty employer cohort.
        calls_before = len(self.api.calls)
        def missing_inventory_request(method, path, **kwargs):
            if path == '/company-sources/view':
                return {'company_sources': []}
            if path == '/profiles/profile/job-inventory':
                return None
            return self.api.request(method, path, **kwargs)
        missing = run('http://127.0.0.1:18700', baseline,
            self.path.with_name('missing-inventory.json'), acceptance_run_id='campaign',
            request=missing_inventory_request, execution_scope='CONTROLLED_TRANSPORT',
            containment_preflight=lambda *_: deepcopy(PROOF), employer_verification=True,
            utc_now=lambda: datetime.fromisoformat(STAMP))
        self.assertEqual(missing['result'], 'FAIL')
        self.assertEqual(missing['error_code'], 'EMPLOYER_BASELINE_INVENTORY_INVALID')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.api.calls[calls_before:]))
        self.assertEqual(self.api.profile, before)

    def test_unknown_or_untrusted_exception_text_is_never_saved_as_containment_diagnostic(self):
        from cli.acceptance_containment import ContainmentError

        class ForeignError(ContainmentError):
            pass

        secret = 'Bearer synthetic-secret-never-serialize'
        for error in (RuntimeError('CHILD_OPERATION_CHANGED'), ForeignError('CHILD_OPERATION_CHANGED'),
                      ContainmentError('UNKNOWN_CODE'), ContainmentError(secret),
                      ContainmentError('CHILD_OPERATION_CHANGED', secret),
                      ContainmentError({'token': secret})):
            with self.subTest(error_class=type(error).__name__):
                self.api = ControlledAPI()

                def containment(stage, context):
                    if stage == 'poll':
                        raise error
                    return deepcopy(PROOF)

                result = self.observe(containment_preflight=containment)
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'PUBLIC_OBSERVATION_API_OR_RECORD_INVALID')
                self.assertNotIn('containment_failure', result)
                self.assertNotIn(secret, json.dumps(result))
                self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')

    def test_one_collection_records_distinct_ids_and_restores_new_source(self):
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['observed_real_job_ids'], ['real-job'])
        self.assertEqual(result['selected_application_job_id'], 'fixture-job')
        self.assertEqual(result['operation_id'], 'original-operation')
        self.assertEqual(result['collection_operation_id'], 'public-operation')
        self.assertEqual(len(result['collection_operation_sha256']), 64)
        self.assertEqual(len(result['source_record_sha256']), 64)
        self.assertEqual(result['acceptance_observations'][0]['status'], 'NOT_RUN')
        self.assertEqual(result['cleanup']['status'], 'RESTORED_DISABLED')
        self.assertEqual(self.api.source['rate_limit'], 5)
        self.assertEqual(self.api.source['concurrency'], 1)
        self.assertEqual(self.api.source['request_timeout'], 10)
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(json.loads(self.path.read_text()), result)
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)

    def child(self, state='RUNNING'):
        return dict(request_id='child-request', revision=2, company_id=self.api.public['company_id'], company_source_id='company-source',
            profile_id='profile', profile_version=VERSION, source_id='9999999999999999999999999999999999999999999999999999999999999999',
            parent_operation_id='public-operation', parent_operation_attempt=1, trigger_job_id='real-job',
            candidate_id='candidate', posting_id=self.api.public['postings'][0]['id'], snapshot_id='snapshot',
            snapshot_sha256=self.api.public['postings'][0]['snapshots'][0]['sha256'],
            quality_observation_id='quality', operation_id='child-operation', state=state,
            authorization='AGGREGATOR_BOOTSTRAP_PENDING', reason=None, operation_attempt=1,
            operation_sha256='a' * 64, pending_sha256='b' * 64)

    def domain_fixture(self, *, enrolled=False, version=1):
        """Controlled API metadata; it never represents a public HTTP observation."""
        self.api.domain_version = version
        child = self.child('COMPLETED')
        self.api.children = [child]
        source = self.api.configured()
        source["rate_limit"] = 5  # The absent primary is created with this fixed policy.
        self.api.source = None
        proof = dict(company_id=child['company_id'], request_id='child-request', request_revision=2,
            operation_id='child-operation', domain_id='domain', domain='acme.example',
            domain_verification_method='FIRST_PARTY_JOB_LINK', domain_evidence_sha256='1' * 64,
            corroboration_method='AGGREGATOR_NATIVE_BOOTSTRAP_V1', execution_scope='CONTROLLED_TRANSPORT',
            execution_scope_basis='PERSISTED_RECEIPT_METADATA',
            native=dict(provider='greenhouse', tenant='acme', job_id='1001',
                native_response_sha256='2' * 64, native_json_sha256='3' * 64),
            pending_sha256=child['pending_sha256'], receipt_sha256='4' * 64,
            envelope_sha256='5' * 64, raw_sha256=child['snapshot_sha256'], normalized_sha256='6' * 64,
            source_snapshot_sha256=_digest(dict(company_id=child['company_id'], provider='wwr', tenant='default',
                endpoint='https://weworkremotely.com/remote-jobs.rss', collector_source_id='9999999999999999999999999999999999999999999999999999999999999999')),
            source_policy_sha256=_digest({key: source[key] for key in ('id', 'provider', 'tenant', 'strategy',
                'capabilities', 'enabled', 'request_timeout', 'rate_limit', 'concurrency')}),
            employer_response_sha256='7' * 64, apply_response_sha256=None, audit_id='domain-audit',
            audit_sha256='8' * 64, acquired_at=STAMP)
        if version == 2:
            from packages.domain.employer_verification import verification_policy_snapshot
            self.api.domain_version = 2
            policy = verification_policy_snapshot(dict(id='profile', version=VERSION,
                preferences={'company_discovery': {'company_discovery_enabled': True,
                    'employer_verification': {'version': 1, 'mode': 'GREENHOUSE_RECIPROCAL_V1',
                                              'activation_id': 'a' * 32}}}))
            proof.update(corroboration_method='AGGREGATOR_NATIVE_BOOTSTRAP_V2',
                verification_policy=policy, verification_policy_sha256=_digest(policy))
        proof['sha256'] = _digest(proof)
        self.api.domain_proofs[child['request_id']] = proof
        if enrolled:
            policy = {key: source[key] for key in ('id', 'provider', 'tenant', 'enabled', 'poll_interval',
                'concurrency', 'request_timeout', 'rate_limit', 'jitter_seconds', 'strategy', 'capabilities')}
            policy.update(id='native-source', provider='greenhouse', tenant='acme', strategy='API')
            self.api.enrolled_sources = [dict(source_id='native-source', company_source_id='native-company-source',
                operation_id=child['operation_id'], request_id=child['request_id'], policy=policy,
                policy_sha256=_digest(policy), audit_id='enrollment-audit', audit_sha256='9' * 64,
                pending_sha256=proof['pending_sha256'], receipt_sha256=proof['receipt_sha256'],
                envelope_sha256=proof['envelope_sha256'])]
        return proof

    def test_domain_metadata_is_captured_once_before_cleanup_and_stays_controlled(self):
        expected = self.domain_fixture()
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        capture = result['domain_proof_readback']
        self.assertEqual(capture['status'], 'CAPTURED')
        self.assertEqual(capture['basis'], 'PERSISTED_RECEIPT_METADATA')
        response = capture['response']
        self.assertEqual(response['observations'][0]['proof'], expected)
        self.assertEqual(capture['response_sha256'], response['sha256'])
        self.assertEqual(response['collection_readback_sha256'], result['child_discovery']['readback_sha256'])
        self.assertNotEqual(expected['source_policy_sha256'], result['source_policy_sha256'])
        domain_calls = [i for i, (_, path, _) in enumerate(self.api.calls) if path.endswith('/domain-proofs')]
        self.assertEqual(len(domain_calls), 1)
        disabled = [i for i, (method, path, kwargs) in enumerate(self.api.calls)
                    if method == 'PUT' and path == '/sources' and kwargs['json']['enabled'] is False]
        self.assertTrue(disabled and domain_calls[0] < disabled[0])
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)
        self.assertEqual(result['execution_scope'], 'CONTROLLED_TRANSPORT')
        self.assertEqual([(row['step'], row['status']) for row in result['acceptance_observations']], [(11, 'NOT_RUN')])
        self.assertEqual(result['selected_application_job_id'], 'fixture-job')
        self.assertNotIn('acceptance_observations', response)
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(json.loads(self.path.read_text()), result)

    def test_v2_domain_metadata_remains_controlled_and_adds_no_numbered_acceptance(self):
        expected = self.domain_fixture(enrolled=True, version=2)
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        response = result['domain_proof_readback']['response']
        self.assertEqual(response['schema_version'], 2)
        self.assertEqual(response['observations'][0]['proof'], expected)
        self.assertEqual(expected['execution_scope'], 'CONTROLLED_TRANSPORT')
        self.assertEqual([(row['step'], row['status']) for row in result['acceptance_observations']], [(11, 'NOT_RUN')])
        self.assertNotIn('acceptance_observations', response)
        self.assertEqual(sum(path.endswith('/domain-proofs') for _, path, _ in self.api.calls), 1)

    def test_v2_unavailable_metadata_stays_null_and_cannot_supply_policy_authority(self):
        self.api.children = [self.child('BLOCKED')]
        self.api.domain_version = 2
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        response = result['domain_proof_readback']['response']
        self.assertEqual(response['schema_version'], 2)
        self.assertIsNone(response['observations'][0]['proof'])
        self.assertEqual(response['observations'][0]['status'], 'UNAVAILABLE')
        self.assertEqual(result['child_discovery']['status'], 'BLOCKED')

    def test_v2_policy_and_version_substitutions_fail_closed_after_rehash(self):
        variants = ['version-bool', 'version-string', 'unknown-version', 'wrong-method',
            'missing-policy', 'extra-policy', 'rate-bool', 'higher-rate', 'wrong-profile',
            'wrong-profile-version', 'bad-activation', 'wrong-policy-digest']
        for variant in variants:
            with self.subTest(variant=variant):
                self.api = ControlledAPI()
                proof = self.domain_fixture(version=2)
                if variant == 'version-bool': self.api.domain_version = True
                elif variant == 'version-string': self.api.domain_version = '2'
                elif variant == 'unknown-version': self.api.domain_version = 3
                elif variant == 'wrong-method': proof['corroboration_method'] = 'AGGREGATOR_NATIVE_BOOTSTRAP_V1'
                elif variant == 'missing-policy': proof.pop('verification_policy')
                elif variant == 'extra-policy': proof['verification_policy']['extra'] = True
                elif variant == 'rate-bool': proof['verification_policy']['rate_limit'] = True
                elif variant == 'higher-rate': proof['verification_policy']['rate_limit'] = 60
                elif variant == 'wrong-profile': proof['verification_policy']['profile_id'] = 'foreign-profile'
                elif variant == 'wrong-profile-version': proof['verification_policy']['profile_version'] = 'a' * 64
                elif variant == 'bad-activation': proof['verification_policy']['activation_id'] = 'old'
                elif variant == 'wrong-policy-digest': proof['verification_policy_sha256'] = 'f' * 64
                if 'verification_policy' in proof and variant != 'wrong-policy-digest':
                    proof['verification_policy_sha256'] = _digest(proof['verification_policy'])
                proof['sha256'] = _digest({k: v for k, v in proof.items() if k != 'sha256'})
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertIsNone(result['domain_proof_readback']['response'])
                self.assertEqual(result['domain_proof_readback']['status'], 'FAIL')

    def test_domain_metadata_retains_missing_proof_without_promoting_it(self):
        self.api.children = [self.child('COMPLETED')]
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        capture = result['domain_proof_readback']
        self.assertEqual(capture['status'], 'CAPTURED')
        observation = capture['response']['observations'][0]
        self.assertEqual(observation['status'], 'UNAVAILABLE')
        self.assertEqual(observation['reason'], 'BOOTSTRAP_RECEIPT_NOT_PERSISTED')
        self.assertIsNone(observation['proof'])
        self.assertEqual([row['step'] for row in result['acceptance_observations']], [11])

    def test_domain_metadata_joins_exact_enrolled_source_receipts(self):
        self.domain_fixture(enrolled=True)
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        row = result['domain_proof_readback']['response']['observations'][0]
        self.assertEqual(row['enrolled_sources'][0]['source_id'], 'native-source')
        self.assertEqual(row['enrolled_sources'][0]['receipt_sha256'], row['proof']['receipt_sha256'])
        self.assertNotIn('policy', row['enrolled_sources'][0])

    def test_changed_domain_metadata_is_rejected_even_with_recomputed_hashes(self):
        changes = [
            (('profile_id',), 'other-profile'), (('profile_version',), '0' * 64),
            (('source_id',), 'other-source'), (('parent_operation_id',), 'other-operation'),
            (('parent_operation_attempt',), True), (('parent_request_id',), 'other-request'),
            (('collection_lineage_sha256',), '0' * 64), (('collection_readback_sha256',), '0' * 64),
            (('observations', 0, 'child', 'company_id'), 'other-company'),
            (('observations', 0, 'child', 'operation_attempt'), True),
            (('observations', 0, 'proof', 'company_id'), 'other-company'),
            (('observations', 0, 'proof', 'request_id'), 'other-request'),
            (('observations', 0, 'proof', 'request_revision'), True),
            (('observations', 0, 'proof', 'operation_id'), 'other-operation'),
            (('observations', 0, 'proof', 'pending_sha256'), '0' * 64),
            (('observations', 0, 'proof', 'raw_sha256'), '0' * 64),
            (('observations', 0, 'proof', 'source_snapshot_sha256'), '0' * 64),
            (('observations', 0, 'proof', 'source_policy_sha256'), '0' * 64),
            (('observations', 0, 'proof', 'receipt_sha256'), '0' * 64),
            (('observations', 0, 'proof', 'native', 'tenant'), 'another-tenant'),
            (('observations', 0, 'proof', 'execution_scope'), 'LIVE_PUBLIC_READ_ONLY'),
            (('observations', 0, 'proof', 'execution_scope_basis'), 'VERIFIED_PUBLIC_HTTP'),
            (('observations', 0, 'proof', 'domain'), 'https://acme.example/private'),
            (('observations', 0, 'proof', 'apply_response_sha256'), 'not-a-hash'),
            (('observations', 0, 'proof', 'acquired_at'), '2026-11-06T12:00:00'),
            (('observations', 0, 'enrolled_sources', 0, 'request_id'), 'different-request'),
            (('observations', 0, 'proof', 'private_html'), 'PRIVATE_MARKER'),
            (('observations', 0, 'proof', 'native', 'url'), 'PRIVATE_MARKER'),
            (('private_payload',), 'PRIVATE_MARKER'),
        ]
        for keys, replacement in changes:
            with self.subTest(field=keys):
                self.api = ControlledAPI()
                self.domain_fixture(enrolled=True)
                def mutate(value):
                    target = value
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = replacement
                    proof = value['observations'][0]['proof']
                    proof['sha256'] = _digest({key: val for key, val in proof.items() if key != 'sha256'})
                    value['sha256'] = _digest({key: val for key, val in value.items() if key != 'sha256'})
                    return value
                self.api.domain_response_transform = mutate
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL', result)
                self.assertEqual(result['domain_proof_readback']['status'], 'FAIL')
                self.assertIsNone(result['domain_proof_readback']['response'])
                self.assertNotIn('PRIVATE_MARKER', json.dumps(result))
                self.assertFalse(self.api.source['enabled'])
                self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)

    def test_domain_response_or_proof_checksum_and_duplicate_rows_fail_closed(self):
        for change in ('response-hash', 'proof-hash', 'duplicate', 'missing', 'unavailable-with-proof'):
            with self.subTest(change=change):
                self.api = ControlledAPI()
                self.domain_fixture()
                def corrupt(value):
                    if change == 'missing':
                        return None
                    if change == 'response-hash':
                        value['sha256'] = '0' * 64
                        return value
                    if change == 'proof-hash':
                        value['observations'][0]['proof']['sha256'] = '0' * 64
                    if change == 'duplicate':
                        value['observations'] *= 2
                    if change == 'unavailable-with-proof':
                        value['observations'][0].update(status='UNAVAILABLE', reason='BOOTSTRAP_RECEIPT_NOT_PERSISTED')
                    value['sha256'] = _digest({key: val for key, val in value.items() if key != 'sha256'})
                    return value
                self.api.domain_response_transform = corrupt
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL', result)
                self.assertIsNone(result['domain_proof_readback']['response'])
                self.assertFalse(self.api.source['enabled'])
                self.assertEqual(sum(path.endswith('/domain-proofs') for _, path, _ in self.api.calls), 1)

    def test_domain_read_containment_loss_preserves_source_and_does_not_save_response(self):
        self.domain_fixture()
        lost = [False]
        def readback(value):
            lost[0] = True
            return value
        self.api.domain_response_transform = readback
        result = self.observe(containment_preflight=lambda *_: dict(PROOF, scheduler_contained=not lost[0]))
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertIsNone(result['domain_proof_readback']['response'])
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
        self.assertTrue(self.api.source['enabled'])

    def test_controlled_domain_capture_cannot_satisfy_live_journey_validator(self):
        from cli.acceptance_journey import validate_public_artifact
        self.domain_fixture()
        result = self.observe()
        self.assertEqual(result['domain_proof_readback']['status'], 'CAPTURED')
        with self.assertRaisesRegex(ValueError, 'Same-profile public observation'):
            validate_public_artifact(result, BASELINE, 'wwr', 'campaign')

    def test_exact_child_finishes_while_source_and_lease_remain_active(self):
        self.api.children = [self.child()]
        def advance():
            self.assertTrue(self.api.source['enabled'])
            if self.api.child_polls == 2:
                self.api.children[0]['state'] = 'COMPLETED'
        self.api.after_child_poll = advance
        result = self.observe(sleep=lambda _: None)
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['child_discovery']['status'], 'COMPLETED')
        self.assertEqual(self.api.child_polls, 2)
        self.assertFalse(self.api.source['enabled'])
        self.assertFalse(any(path == '/operations' for _, path, _ in self.api.calls))

    def test_empty_complete_child_set_is_not_run(self):
        contexts = []
        def contain(stage, context):
            contexts.append((stage, deepcopy(context)))
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=contain)
        self.assertEqual(result['child_discovery']['status'], 'NOT_RUN')
        self.assertEqual(result['child_discovery']['children'], [])
        self.assertEqual(result['domain_proof_readback']['status'], 'NOT_RUN')
        self.assertEqual(result['domain_proof_readback']['reason'], 'NO_CURRENT_COLLECTION_CHILDREN')
        self.assertFalse(any(path.endswith('/domain-proofs') for _, path, _ in self.api.calls))
        final_contexts = [context for stage, context in contexts if stage in {'finalize', 'cleanup'}]
        self.assertTrue(final_contexts)
        self.assertTrue(all(context['child_discovery_requested'] is True for context in final_contexts))
        self.assertTrue(all(context['owned_child_operation_ids'] == [] for context in final_contexts))
        self.assertTrue(all(context['owned_child_lineage_sha256'] == _digest([]) for context in final_contexts))

    def test_blocked_child_is_explicit_and_does_not_gain_acceptance_credit(self):
        self.api.children = [self.child('BLOCKED')]
        self.api.children[0]['reason'] = 'ROBOTS_DENIED'
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['child_discovery']['status'], 'BLOCKED')
        self.assertEqual(result['child_discovery']['children'][0]['reason'], 'ROBOTS_DENIED')
        self.assertEqual([row['step'] for row in result['acceptance_observations']], [11])

    def test_child_timeout_is_bounded_and_cleanup_revokes_pending_source(self):
        self.api.children = [self.child()]
        ticks = iter(range(100))
        result = self.observe(child_timeout_seconds=2, monotonic=lambda: next(ticks), sleep=lambda _: None)
        self.assertEqual(result['child_discovery']['status'], 'BLOCKED')
        self.assertEqual(result['child_discovery']['reason'], 'CHILD_DISCOVERY_TIMEOUT')
        self.assertEqual(result['domain_proof_readback']['status'], 'NOT_RUN')
        self.assertEqual(result['domain_proof_readback']['reason'], 'CHILDREN_NOT_SETTLED')
        self.assertFalse(any(path.endswith('/domain-proofs') for _, path, _ in self.api.calls))
        self.assertFalse(self.api.source['enabled'])

    def test_child_lineage_or_incomplete_response_is_never_admitted(self):
        for change in ('parent', 'profile', 'source', 'snapshot', 'incomplete', 'replacement'):
            with self.subTest(change=change):
                self.api = ControlledAPI()
                self.api.children = [self.child()]
                row = self.api.children[0]
                if change == 'parent': row['parent_operation_id'] = 'other'
                if change == 'profile': row['profile_version'] = 'c' * 64
                if change == 'source': row['source_id'] = 'other'
                if change == 'snapshot': row['snapshot_sha256'] = 'c' * 64
                if change == 'incomplete': self.api.children_complete = False
                if change == 'replacement':
                    def replace():
                        if self.api.child_polls == 2:
                            self.api.children[0]['operation_id'] = 'unrelated'
                    self.api.after_child_poll = replace
                result = self.observe(sleep=lambda _: None)
                self.assertEqual(result['result'], 'FAIL', result)
                self.assertNotEqual(result['child_discovery']['status'], 'COMPLETED')
                self.assertFalse(self.api.source['enabled'])

    def test_injected_request_cannot_claim_live_scope_even_by_default(self):
        for options in ({}, {'execution_scope': 'LIVE_PUBLIC_READ_ONLY'}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'TRANSPORT_SCOPE'):
                run('http://127.0.0.1:18700', BASELINE, self.path, acceptance_run_id='campaign',
                    request=self.api.request, containment_preflight=lambda *_: deepcopy(PROOF), **options)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.path.exists())

    def test_containment_context_uses_only_dispatched_collection_identity(self):
        contexts = []
        def containment(stage, context):
            dispatched = [kwargs['json'] for method, path, kwargs in self.api.calls
                if method == 'POST' and path.endswith('/collect')]
            contexts.append((stage, deepcopy(context), deepcopy(dispatched)))
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'PASS', result)
        self.assertTrue(any(not dispatched for _, _, dispatched in contexts))
        self.assertTrue(any(dispatched for _, _, dispatched in contexts))
        for _stage, context, dispatched in contexts:
            self.assertIn('collection_operation_id', context)
            self.assertIn('collection_request_id', context)
            self.assertEqual(context['collection_operation_id'], 'public-operation' if dispatched else None)
            self.assertEqual(context['collection_request_id'], dispatched[0]['request_id'] if dispatched else None)
        self.assertNotEqual(result['collection_operation_id'], BASELINE['operation_id'])
        self.assertNotEqual(result['collection_request_id'], BASELINE['request_id'])

    def test_unconfirmed_dispatch_does_not_invent_callback_identity(self):
        original = self.api.request
        contexts = []
        def failed_dispatch(method, path, **kwargs):
            if method == 'POST' and path.endswith('/collect'):
                raise RuntimeError('No confirmed response')
            return original(method, path, **kwargs)
        self.api.request = failed_dispatch
        def containment(_stage, context):
            contexts.append(deepcopy(context))
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'FAIL')
        self.assertTrue(contexts)
        self.assertTrue(all('collection_operation_id' in row and row['collection_operation_id'] is None
            and 'collection_request_id' in row and row['collection_request_id'] is None for row in contexts))

    def test_existing_evidence_is_preserved_before_any_request(self):
        self.path.write_text('prior receipt', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.observe()
        self.assertEqual(self.path.read_text(), 'prior receipt')
        self.assertEqual(self.api.calls, [])

    def test_symlink_evidence_or_parent_is_rejected_before_any_request(self):
        for parent_link in (False, True):
            with self.subTest(parent_link=parent_link):
                target = Path(self.directory) / ('target-directory' if parent_link else 'missing.json')
                link = Path(self.directory) / ('linked-directory' if parent_link else 'linked.json')
                if parent_link:
                    target.mkdir()
                try:
                    link.symlink_to(target, target_is_directory=parent_link)
                except OSError as error:
                    self.skipTest('This Windows account cannot create symlinks: ' + str(error.winerror))
                self.path = link / 'receipt.json' if parent_link else link
                with self.assertRaises((ValueError, FileExistsError)):
                    self.observe()
                self.assertTrue(link.is_symlink())
                self.assertEqual(self.api.calls, [])

    def test_late_evidence_collision_is_preserved_after_source_cleanup(self):
        self.api.after_collection = lambda: self.path.write_text('another receipt', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.observe()
        self.assertEqual(self.path.read_text(), 'another receipt')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    def test_atomic_publication_cannot_overwrite_a_last_instant_collision(self):
        original = os.link
        def raced(source, destination):
            Path(destination).write_text('concurrent receipt', encoding='utf-8')
            return original(source, destination)
        with patch('cli.acceptance_public_source.os.link', side_effect=raced), self.assertRaises(FileExistsError):
            self.observe()
        self.assertEqual(self.path.read_text(), 'concurrent receipt')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(list(Path(self.directory).iterdir()), [self.path])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction contract')
    def test_junction_parent_is_rejected_before_any_request(self):
        target = Path(self.directory) / 'target-directory'
        target.mkdir()
        junction = Path(self.directory) / 'junction-directory'
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
            capture_output=True, text=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.path = junction / 'public.json'
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_PARENT_INVALID'):
            self.observe()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(list(target.iterdir()), [])

    def test_absent_expired_or_short_lease_blocks_before_enable(self):
        for expiry in (None, '2026-11-06T11:59:59+00:00', '2026-11-06T12:00:10+00:00'):
            with self.subTest(expiry=expiry):
                self.api = ControlledAPI()
                proof = dict(PROOF, lease_expires_at=expiry)
                result = self.observe(containment_preflight=lambda *_: proof)
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_INVALID')
                self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_containment_is_checked_between_polls_and_loss_cancels_once(self):
        self.api.state = 'RUNNING'
        heartbeat = {'lost': False}
        clock = [0.0]
        def sleep(_seconds):
            heartbeat['lost'] = True
            clock[0] += _seconds
        result = self.observe(sleep=sleep, timeout_seconds=2, monotonic=lambda: clock[0],
            containment_preflight=lambda *_: dict(PROOF,
            source_collection_exclusive=not heartbeat['lost']))
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertTrue(result['cancellation_requested'])
        self.assertEqual(sum(path.endswith('/cancel') for _, path, _ in self.api.calls), 1)
        self.assertEqual(sum(path == '/operations/public-operation' for _, path, _ in self.api.calls), 1)
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')

    def test_lease_gap_cannot_be_hidden_by_renewal_after_a_poll(self):
        now = [datetime.fromisoformat(STAMP)]
        original = self.api.request
        def delayed(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if path == '/operations/public-operation':
                now[0] = datetime.fromisoformat('2026-11-06T12:06:00+00:00')
            return value
        self.api.request = delayed
        result = run('http://127.0.0.1:18700', BASELINE, self.path, acceptance_run_id='campaign',
            request=self.api.request, execution_scope='CONTROLLED_TRANSPORT', utc_now=lambda: now[0],
            containment_preflight=lambda *_: dict(PROOF, lease_expires_at=(
                '2026-11-06T12:10:00+00:00' if now[0].minute == 6 else PROOF['lease_expires_at'])))
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_EXPIRED')
        self.assertFalse(any(path == '/jobs/real-job' for _, path, _ in self.api.calls))

    def _delayed_dispatch(self, transform=lambda proof: None, *, with_witness=True):
        """Model a long setup callback with two timely same-owner renewals."""
        self.api = ControlledAPI()
        base = datetime.fromisoformat(STAMP)
        now = [base]
        dispatched = [False]
        def contain(stage, _context):
            proof = dict(PROOF, lease_expires_at=(base + timedelta(seconds=30)).isoformat())
            if dispatched[0]:
                return dict(proof, lease_expires_at=(base + timedelta(seconds=60)).isoformat())
            if stage == 'dispatch':
                dispatched[0] = True
                now[0] = base + timedelta(seconds=40)
                proof['lease_expires_at'] = (base + timedelta(seconds=60)).isoformat()
                if with_witness:
                    proof['lease_continuity'] = {'schema_version': 1, 'checks': [dict(
                        owner=proof['owner'], token_sha256=hashlib.sha256(proof['token'].encode()).hexdigest(),
                        checked_at=(base + timedelta(seconds=checked)).isoformat(),
                        lease_expires_at=(base + timedelta(seconds=expires)).isoformat())
                        for checked, expires in ((0, 30), (20, 60), (40, 60))]}
                transform(proof)
            return proof
        return self.observe(containment_preflight=contain, utc_now=lambda: now[0])

    def test_overlapping_owner_checks_bridge_callback_deadline_without_live_credit(self):
        result = self._delayed_dispatch()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['cleanup']['status'], 'RESTORED_DISABLED')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)
        self.assertEqual(result['acceptance_observations'][0]['status'], 'NOT_RUN')
        records = result['lease_continuity_checks']
        self.assertEqual(len(records), 1)
        check = records[0]
        self.assertEqual(check['stage'], 'dispatch')
        self.assertEqual(check['callback_started_at'], STAMP)
        self.assertEqual(check['previous_expires_at'], '2026-11-06T12:00:30+00:00')
        self.assertEqual(check['checked_at'], '2026-11-06T12:00:40+00:00')
        self.assertEqual(check['lease_expires_at'], '2026-11-06T12:01:00+00:00')
        self.assertEqual(check['sha256'], _digest(check['witness']))
        self.assertEqual(check['witness']['checks'][-1]['token_sha256'],
                         hashlib.sha256(PROOF['token'].encode()).hexdigest())
        self.assertNotIn(PROOF['token'], json.dumps(result))

    def test_renewed_expiry_without_continuity_cannot_bridge_callback_deadline(self):
        result = self._delayed_dispatch(with_witness=False)
        self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_EXPIRED')
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
        self.assertTrue(self.api.source['enabled'])
        self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_invalid_callback_continuity_cannot_dispatch_or_replace_cached_proof(self):
        invalid = 'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID'
        expired = 'SOURCE_CONTAINMENT_LEASE_EXPIRED'
        def witness(value):
            return lambda proof: proof.update(lease_continuity=value)
        def row(index, key, value):
            return lambda proof: proof['lease_continuity']['checks'][index].update({key: value})
        cases = [
            ('absent-body', witness(None), invalid),
            ('wrong-body-type', witness([]), invalid),
            ('empty', witness({'schema_version': 1, 'checks': []}), invalid),
            ('boolean-version', lambda p: p['lease_continuity'].update(schema_version=True), invalid),
            ('unknown-version', lambda p: p['lease_continuity'].update(schema_version=2), invalid),
            ('extra-field', lambda p: p['lease_continuity'].update(untrusted=True), invalid),
            ('missing-field', lambda p: p['lease_continuity'].pop('schema_version'), invalid),
            ('too-many', lambda p: p['lease_continuity'].update(checks=
                [deepcopy(p['lease_continuity']['checks'][0])] * 129), invalid),
            ('row-type', lambda p: p['lease_continuity']['checks'].__setitem__(1, None), invalid),
            ('row-extra', row(1, 'untrusted', True), invalid),
            ('row-missing', lambda p: p['lease_continuity']['checks'][1].pop('owner'), invalid),
            ('owner-change', row(1, 'owner', 'another-owner'), invalid),
            ('token-change', row(1, 'token_sha256', '0' * 64), invalid),
            ('json-token-hash', row(1, 'token_sha256', _digest(PROOF['token'])), invalid),
            ('bad-time', row(1, 'checked_at', 'invalid'), invalid),
            ('naive-time', row(1, 'checked_at', '2026-11-06T12:00:20'), invalid),
            ('offset-time', row(1, 'checked_at', '2026-11-06T13:00:20+01:00'), invalid),
            ('non-string-time', row(1, 'checked_at', True), invalid),
            ('future-check', row(2, 'checked_at', '2026-11-06T12:00:41+00:00'), invalid),
            ('prior-callback', row(0, 'checked_at', '2026-11-06T11:59:59+00:00'), invalid),
            ('reordered', row(2, 'checked_at', '2026-11-06T12:00:19+00:00'), invalid),
            ('at-expiry', row(1, 'checked_at', '2026-11-06T12:00:30+00:00'), expired),
            ('after-expiry', row(1, 'checked_at', '2026-11-06T12:00:31+00:00'), expired),
            ('missing-bridge', lambda p: p['lease_continuity']['checks'].pop(1), expired),
            ('expiry-regression', row(1, 'lease_expires_at', '2026-11-06T12:00:29+00:00'), invalid),
            ('expired-check', row(1, 'lease_expires_at', '2026-11-06T12:00:20+00:00'), expired),
            ('terminal-mismatch', lambda p: p.update(lease_expires_at='2026-11-06T12:01:30+00:00'), invalid),
            ('stale-final-proof', lambda p: p.update(lease_expires_at='2026-11-06T12:00:30+00:00'),
             'SOURCE_CONTAINMENT_LEASE_INVALID'),
            ('short-final-proof', lambda p: p.update(lease_expires_at='2026-11-06T12:00:55+00:00'),
             'SOURCE_CONTAINMENT_LEASE_INVALID'),
            ('final-owner-change', lambda p: p.update(owner='another-owner'), 'SOURCE_OWNERSHIP_CHANGED'),
            ('final-token-change', lambda p: p.update(token='another-token'), 'SOURCE_OWNERSHIP_CHANGED'),
        ]
        for name, change, code in cases:
            with self.subTest(case=name):
                result = self._delayed_dispatch(change)
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], code, result)
                self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
                self.assertEqual(result['containment']['lease_expires_at'], '2026-11-06T12:00:30+00:00')
                self.assertNotIn('lease_continuity_checks', result)
                self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_malformed_witness_is_rejected_even_before_previous_deadline(self):
        result = self.observe(containment_preflight=lambda *_: dict(PROOF, lease_continuity={}))
        self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_CONTINUITY_INVALID')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.api.calls))

    def test_expired_entry_cannot_invoke_a_callback_offering_new_continuity(self):
        now = [datetime.fromisoformat(STAMP)]
        original = self.api.request
        callback_times = []
        def delayed(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if path == '/operations/public-operation':
                now[0] += timedelta(minutes=6)
            return value
        def contain(_stage, _context):
            callback_times.append(now[0])
            return dict(PROOF, lease_expires_at=(now[0] + timedelta(minutes=5)).isoformat(),
                lease_continuity={'schema_version': 1, 'checks': [dict(owner=PROOF['owner'],
                    token_sha256=hashlib.sha256(PROOF['token'].encode()).hexdigest(),
                    checked_at=now[0].isoformat(),
                    lease_expires_at=(now[0] + timedelta(minutes=5)).isoformat())]})
        self.api.request = delayed
        result = self.observe(containment_preflight=contain, utc_now=lambda: now[0])
        self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_EXPIRED')
        self.assertTrue(callback_times)
        self.assertTrue(all(value == datetime.fromisoformat(STAMP) for value in callback_times))
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
        self.assertFalse(any(path == '/jobs/real-job' for _, path, _ in self.api.calls))

    def test_continuity_evidence_is_bounded_across_long_callbacks(self):
        now = [datetime.fromisoformat(STAMP)]
        expires = [now[0] + timedelta(seconds=60)]
        def contain(stage, _context):
            proof = dict(PROOF, lease_expires_at=expires[0].isoformat())
            if stage == 'preflight':
                return proof
            old_expiry = expires[0]
            checkpoints = [(now[0], old_expiry)]
            now[0] = old_expiry - timedelta(seconds=5)
            expires[0] = now[0] + timedelta(seconds=60)
            checkpoints.append((now[0], expires[0]))
            now[0] = old_expiry + timedelta(seconds=1)
            checkpoints.append((now[0], expires[0]))
            return dict(proof, lease_expires_at=expires[0].isoformat(),
                lease_continuity={'schema_version': 1, 'checks': [dict(owner=proof['owner'],
                    token_sha256=hashlib.sha256(proof['token'].encode()).hexdigest(),
                    checked_at=checked.isoformat(), lease_expires_at=until.isoformat())
                    for checked, until in checkpoints]})
        result = self.observe(containment_preflight=contain, utc_now=lambda: now[0])
        self.assertEqual(result['error_code'], 'SOURCE_CONTAINMENT_LEASE_CONTINUITY_LIMIT')
        self.assertEqual(len(result['lease_continuity_checks']), 16)
        self.assertEqual(result['containment']['lease_expires_at'],
                         result['lease_continuity_checks'][-1]['lease_expires_at'])
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
        self.assertNotIn(PROOF['token'], json.dumps(result))

    def test_lost_containment_at_public_list_prevents_detail_reads(self):
        original = self.api.request
        lost = [False]
        def listed(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if path == '/jobs':
                lost[0] = True
            return value
        self.api.request = listed
        result = self.observe(containment_preflight=lambda *_: dict(PROOF, scheduler_contained=not lost[0]))
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertFalse(any(path == '/jobs/real-job' for _, path, _ in self.api.calls))

    def test_public_rows_must_be_a_list_of_at_most_five_before_detail_reads(self):
        for rows in ([{'id': 'real-job'}] * 6, {'id': 'real-job'}):
            with self.subTest(rows=rows):
                self.api = ControlledAPI()
                original = self.api.request
                def oversized(method, path, **kwargs):
                    value = original(method, path, **kwargs)
                    return {'jobs': rows} if path == '/jobs' else value
                self.api.request = oversized
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'PUBLIC_JOB_RESPONSE_INVALID')
                self.assertFalse(any(path == '/jobs/real-job' for _, path, _ in self.api.calls))

    def test_exactly_five_public_jobs_are_allowed(self):
        original = self.api.request
        jobs = {f'real-job-{number}': dict(deepcopy(self.api.public), id=f'real-job-{number}')
                for number in range(5)}
        from packages.persistence.store import identity
        for number, job in enumerate(jobs.values()):
            native = 'provider-job-' + str(number)
            posting = job['postings'][0]
            posting['job_id'] = job['id']
            posting['raw'].update(guid='https://weworkremotely.com/remote-jobs/' + native,
                link='https://weworkremotely.com/remote-jobs/' + native)
            posting['source_job_id'] = identity(job['company_id'], native)
            posting['id'] = identity(self.api.public['attributes']['source_id'], posting['source_job_id'])
            posting['snapshots'][0]['sha256'] = _digest(posting['raw'])
            job['attributes']['native_posting_id'] = native
        def five(method, path, **kwargs):
            if method == 'GET' and path.removeprefix('/jobs/') in jobs:
                return jobs[path.removeprefix('/jobs/')]
            value = original(method, path, **kwargs)
            return {'jobs': list(jobs.values())} if path == '/jobs' else value
        self.api.request = five
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertEqual(result['observed_real_job_ids'], sorted(jobs))

    def test_missing_containment_blocks_before_any_write(self):
        result = self.observe(containment_preflight=lambda *_: {'scheduler_contained': False})
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_scheduler_pause_without_exclusive_collection_owner_is_insufficient(self):
        result = self.observe(containment_preflight=lambda *_: dict(PROOF, source_collection_exclusive=False))
        self.assertEqual(result['error_code'], 'SCHEDULER_CONTAINMENT_REQUIRED')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_policy_change_at_dispatch_cannot_start_collection(self):
        self.api.configured()
        def containment(stage, _context):
            if stage == 'dispatch':
                self.api.source['request_timeout'] = 12
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['error_code'], 'SOURCE_CONFIGURATION_CHANGED')
        self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_ambiguous_enable_response_requires_containment_to_remain_held(self):
        original = self.api.request
        def ambiguous(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if method == 'PUT':
                raise RuntimeError('Connection ended after remote configuration committed')
            return value
        self.api.request = ambiguous
        result = self.observe()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['cleanup']['status'], 'ENABLE_UNCONFIRMED_PRESERVE_CONTAINMENT')
        self.assertTrue(self.api.source['enabled'])
        self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_created_policy_constraint_precedes_reads_without_constructing_http_clients(self):
        from cli.acceptance_public_source import _policy_fingerprint

        contexts, created = [], {}
        original = self.api.request

        def transport(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if method == 'PUT' and kwargs['json']['enabled'] is True:
                created.update(deepcopy(self.api.source))
            return value

        def containment(stage, context):
            contexts.append((stage, deepcopy(context)))
            if context['source_id'] is not None:
                self.assertEqual(context['source_policy_sha256'], _policy_fingerprint(created))
            return deepcopy(PROOF)

        self.api.request = transport
        with patch('httpx.Client', side_effect=AssertionError('metadata must not create a client')) as client:
            with patch('httpx.AsyncClient', side_effect=AssertionError('metadata must not create a client')) as async_client:
                result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'PASS', result)
        client.assert_not_called()
        async_client.assert_not_called()
        bound = [(stage, context) for stage, context in contexts if context['source_id'] is not None]
        self.assertEqual([stage for stage, _ in bound[:2]], ['read', 'read'])
        self.assertTrue(all(context['newly_created'] is True for _, context in bound))
        self.assertEqual(result['source_policy_sha256'], _policy_fingerprint(created))

    def test_malformed_creation_identity_preserves_ambiguous_enable_without_dispatch(self):
        for response in (None, [], {}, {'id': None}, {'id': ''}, {'id': 1}, {'id': True},
                         {'id': []}, {'id': '../source'}, {'id': 'a' * 129}):
            with self.subTest(response=response):
                self.api = ControlledAPI()
                original = self.api.request

                def malformed(method, path, **kwargs):
                    value = original(method, path, **kwargs)
                    return deepcopy(response) if method == 'PUT' else value

                self.api.request = malformed
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'SOURCE_CONFIGURATION_CHANGED')
                self.assertEqual(result['cleanup']['status'], 'ENABLE_UNCONFIRMED_PRESERVE_CONTAINMENT')
                self.assertNotIn('source_id', result)
                self.assertTrue(self.api.source['enabled'])
                self.assertEqual(sum(method == 'PUT' for method, _, _ in self.api.calls), 1)
                self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_unverified_creation_readback_never_authorizes_cleanup_or_dispatch(self):
        changes = [('id', 'foreign-source'), ('strategy', 'API'), ('capabilities', {}),
                   ('enabled', 1), ('concurrency', True), ('request_timeout', 11), ('rate_limit', 2)]
        for field, value in changes:
            with self.subTest(field=field):
                self.api = ControlledAPI()
                original = self.api.request
                changed = False

                def readback(method, path, **kwargs):
                    nonlocal changed
                    response = original(method, path, **kwargs)
                    if (method, path) == ('GET', '/sources') and self.api.source and not changed:
                        changed = True
                        response['sources'][0][field] = value
                    return response

                self.api.request = readback
                result = self.observe()
                self.assertTrue(changed)
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'SOURCE_CONFIGURATION_CHANGED')
                self.assertEqual(result['cleanup']['status'], 'PRESERVED_POLICY_CHANGED')
                self.assertTrue(self.api.source['enabled'])
                self.assertEqual(sum(method == 'PUT' for method, _, _ in self.api.calls), 1)
                self.assertFalse(any(path.endswith('/collect') for _, path, _ in self.api.calls))

    def test_disabled_source_is_not_reenabled_or_reconfigured(self):
        self.api.configured(enabled=False)
        original = deepcopy(self.api.source)
        result = self.observe()
        self.assertEqual(result['error_code'], 'SOURCE_DISABLED')
        self.assertEqual(self.api.source, original)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_existing_enabled_policy_is_used_without_configuration_writes(self):
        self.api.configured()
        result = self.observe()
        self.assertEqual(result['result'], 'PASS', result)
        self.assertTrue(self.api.source['enabled'])
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.api.calls))

    def test_dispatch_preflight_receives_exact_existing_source_identity_and_policy(self):
        self.api.configured()
        def containment(stage, context):
            if stage == 'dispatch':
                self.assertEqual(context['source_id'], '9999999999999999999999999999999999999999999999999999999999999999')
                self.assertEqual(len(context['source_policy_sha256']), 64)
            return deepcopy(PROOF)
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'PASS', result)

    def test_cooldown_or_backoff_prevents_collection(self):
        for field in ('last_attempt', 'backoff_until'):
            with self.subTest(field=field):
                self.api = ControlledAPI()
                self.api.configured()[field] = ('2026-11-07T12:00:00+00:00'
                    if field == 'backoff_until' else STAMP)
                result = self.observe()
                self.assertIn(result['error_code'], {'SOURCE_COOLDOWN', 'SOURCE_BACKOFF'})
                self.assertFalse(any(method != 'GET' for method, _, _ in self.api.calls))

    def test_terminal_provider_failure_is_recorded_without_retry(self):
        self.api.state = 'BLOCKED'
        result = self.observe()
        self.assertEqual(result['error_code'], 'PUBLIC_COLLECTION_BLOCKED')
        self.assertEqual(result['collection_error'], 'SOURCE_UNAVAILABLE')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)

    def test_cancelled_operation_remains_cancelled_without_retry(self):
        self.api.state = 'CANCELLED'
        result = self.observe()
        self.assertEqual(result['error_code'], 'PUBLIC_COLLECTION_CANCELLED')
        self.assertEqual(result['collection_state'], 'CANCELLED')
        self.assertFalse(self.api.source['enabled'])
        self.assertEqual(sum(path.endswith('/collect') for _, path, _ in self.api.calls), 1)

    def test_foreign_operation_and_retried_result_cannot_pass(self):
        for change in ('params', 'attempts'):
            with self.subTest(change=change):
                self.api = ControlledAPI()
                original = self.api.request
                def changed(method, path, **kwargs):
                    value = original(method, path, **kwargs)
                    if method == 'GET' and path == '/operations/public-operation':
                        if change == 'params':
                            value['params'] = dict(value['params'], source_id='foreign-source')
                        else:
                            value['attempt_count'] = 2
                    return value
                self.api.request = changed
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertIn(result['error_code'], {'COLLECTION_IDENTITY_CHANGED', 'PUBLIC_COLLECTION_RETRIED'})

    def test_timeout_requests_cancellation_and_disables_owned_source(self):
        self.api.state = 'RUNNING'
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        result = self.observe(timeout_seconds=2, monotonic=lambda: clock[0], sleep=sleep)
        self.assertEqual(result['error_code'], 'PUBLIC_COLLECTION_TIMEOUT')
        self.assertTrue(result['cancellation_requested'])
        self.assertFalse(self.api.source['enabled'])

    def test_changed_source_policy_is_preserved_during_cleanup(self):
        self.api.after_collection = lambda: self.api.source.update(rate_limit=7)
        result = self.observe()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_POLICY_CHANGED')
        self.assertEqual(self.api.source['rate_limit'], 7)
        self.assertTrue(self.api.source['enabled'])

    def test_existing_source_disablement_during_observation_is_preserved_and_rejected(self):
        self.api.configured()
        self.api.after_collection = lambda: self.api.source.update(enabled=False)
        result = self.observe()
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['error_code'], 'SOURCE_CONFIGURATION_CHANGED')
        self.assertFalse(self.api.source['enabled'])
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.api.calls))

    def test_lost_ownership_preserves_source_and_fails_cleanup(self):
        def containment(stage, _context):
            return dict(PROOF, token='other-owner' if stage == 'cleanup' else PROOF['token'])
        result = self.observe(containment_preflight=containment)
        self.assertEqual(result['result'], 'FAIL')
        self.assertEqual(result['cleanup']['status'], 'PRESERVED_OWNERSHIP_CHANGED')
        self.assertTrue(self.api.source['enabled'])

    def test_selected_job_or_profile_changes_cannot_pass(self):
        for change in ('job', 'profile'):
            with self.subTest(change=change):
                self.api = ControlledAPI()
                self.api.after_collection = (lambda: self.api.app.update(job_id='real-job')) if change == 'job' else (
                    lambda: self.api.profile.update(version='2' * 64))
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'BASELINE_SELECTION_CHANGED')

    def test_snapshot_corruption_and_fictional_jobs_cannot_prove_real_collection(self):
        for field in ('snapshot', 'fictional', 'stale-fetch'):
            with self.subTest(field=field):
                self.api = ControlledAPI()
                if field == 'snapshot':
                    self.api.public['postings'][0]['snapshots'][0]['sha256'] = '0' * 64
                elif field == 'fictional':
                    self.api.public['attributes']['fictional'] = True
                else:
                    self.api.public['postings'][0]['fetched_at'] = '2026-11-05T12:00:00+00:00'
                result = self.observe()
                self.assertEqual(result['result'], 'FAIL')
                self.assertEqual(result['error_code'], 'PUBLIC_JOB_PROVENANCE_INVALID')

    def test_helper_does_not_publish_unstructured_provider_diagnostics(self):
        self.api.state = 'BLOCKED'
        original = self.api.request
        def private_failure(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if method == 'GET' and path == '/operations/public-operation':
                value['error'] = 'PRIVATE_SENTINEL token=value applicant@example.test'
            return value
        self.api.request = private_failure
        result = self.observe()
        self.assertEqual(result['collection_error'], 'UNCLASSIFIED_PROVIDER_FAILURE')
        self.assertNotIn('PRIVATE_SENTINEL', self.path.read_text())

    def test_unsupported_provider_and_nonloopback_api_are_rejected_before_requests(self):
        with self.assertRaises(ValueError):
            self.observe(provider='remoteok')
        self.assertEqual(self.api.calls, [])
        with self.assertRaises(ValueError):
            run('https://external.example', BASELINE, self.path, acceptance_run_id='campaign',
                containment_preflight=lambda *_: deepcopy(PROOF), request=self.api.request)
        self.assertEqual(self.api.calls, [])
