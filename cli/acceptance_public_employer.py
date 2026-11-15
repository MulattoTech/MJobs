"""Read-only, bounded evidence for one public collection's same-employer chain.

This captures prerequisites, never dispatches work or grants numbered acceptance.
The public browser and authoritative journey verifier must separately validate a
whole employer group. Controlled transport remains explicitly NOT_RUN for live
acceptance even when all component joins succeed.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qs, quote, urlsplit

from cli.acceptance_public_source import (
    FINGERPRINT_FIELDS, SAFE_ID, _digest, _require,
    _observe_domain_proofs, _timestamp,
)
from packages.domain.employer_verification import verification_policy_snapshot
from services.company_discovery.aggregator_bootstrap import source_policy_projection
from services.discovery.source_collection_receipts import MAX_BYTES, MAX_ENTRIES, CAPACITY_REASON

MAX_DETAIL_READS = 16
MAX_SOURCE_ROWS = 10_000
SOURCE_FIELDS = ('id', 'company_id', 'company_name', 'profile_id', 'profile_company_associated',
    'official_domain', 'career_url', 'provider', 'tenant', 'collection_strategy', 'enabled',
    'origin', 'discovered_automatically')
MANIFEST_FIELDS = {'canonical_job_id', 'posting_id', 'source_job_id', 'snapshot_id',
                   'snapshot_sha256', 'candidate_id'}
INVENTORY_FIELDS = {'schema_version', 'company_source_id', 'company_id', 'collector_source_id',
    'provider', 'tenant', 'endpoint', 'source_policy_sha256', 'child_operation_id',
    'child_operation_attempt', 'operation_params_sha256', 'discovery_request_id', 'request_revision',
    'parent_operation_id', 'parent_operation_attempt', 'parent_request_id', 'profile_id',
    'profile_version', 'pending_sha256', 'bootstrap_receipt_sha256', 'collection_started_at',
    'quality_observation_id', 'provider_complete_inventory', 'enumerated_count', 'accepted_count',
    'rejected_count', 'status', 'reason', 'manifest', 'manifest_sha256', 'duplicate_count',
    'inventory_complete', 'receipt_sha256', 'audit_id', 'audit_sha256', 'sha256'}


def _sealed(value):
    return {**value, 'sha256': _digest(value)}


def _check_seal(value, code='EMPLOYER_EVIDENCE_CHANGED'):
    _require(isinstance(value, dict) and value.get('sha256') ==
        _digest({k: v for k, v in value.items() if k != 'sha256'}), code)


def _id(value):
    _require(isinstance(value, str) and SAFE_ID.fullmatch(value), 'EMPLOYER_ID_INVALID')
    return value


def _sources(read, profile_id):
    value = read('GET', '/company-sources/view', params={'profile_id': profile_id})
    _require(isinstance(value, dict) and isinstance(value.get('company_sources'), list),
        'EMPLOYER_SOURCE_VIEW_INVALID')
    rows = value['company_sources']
    _require(len(rows) <= MAX_SOURCE_ROWS and all(isinstance(row, dict) for row in rows)
        and len({_id(row.get('id')) for row in rows}) == len(rows), 'EMPLOYER_SOURCE_VIEW_INVALID')
    return rows


def begin(read, profile_id, profile_version, *, utc_now=lambda: datetime.now(timezone.utc)):
    """Capture the complete source view before dispatch; no job-page approximation.

    Absence covers persisted company sources, not orphan company records. A
    caller requesting live observation must also validate its saved UI witness.
    """
    _id(profile_id)
    profile = read('GET', '/profiles/' + quote(profile_id, safe=''))
    policy = verification_policy_snapshot(profile)
    _require(profile.get('id') == profile_id and profile.get('version') == profile_version
        and policy is not None, 'EMPLOYER_PROFILE_CONSENT_CHANGED')
    rows = _sources(read, profile_id)
    minimum_score = (profile.get('preferences') or {}).get('company_discovery', {}).get('minimum_match_for_discovery', 60)
    _require(type(minimum_score) in (int, float) and 0 <= minimum_score <= 100,
        'EMPLOYER_RELEVANCE_POLICY_INVALID')
    return _sealed(dict(schema_version=1, profile_id=profile_id, profile_version=profile_version,
        captured_at=utc_now().isoformat(),
        scope='COMPLETE_PERSISTED_COMPANY_SOURCE_VIEW', policy_sha256=_digest(policy),
        minimum_match_score=minimum_score,
        company_ids=sorted({_id(row.get('company_id')) for row in rows}),
        source_ids=sorted(row['id'] for row in rows), source_view_sha256=_digest(rows)))


def _inventory(response, evidence, source):
    children = evidence['child_discovery']
    _check_seal(response)
    fields = {'schema_version', 'profile_id', 'profile_version', 'source_id', 'parent_operation_id',
        'parent_operation_attempt', 'parent_request_id', 'complete', 'limit',
        'collection_lineage_sha256', 'collection_readback_sha256', 'observations', 'sha256'}
    _require(set(response) == fields and type(response['schema_version']) is int
        and response['schema_version'] == 1 and response['complete'] is True
        and type(response['limit']) is int and response['limit'] == 2, 'EMPLOYER_INVENTORY_INVALID')
    for key in ('profile_id', 'profile_version', 'source_id', 'parent_operation_id', 'parent_operation_attempt'):
        _require(type(response[key]) is type(children[key]) and response[key] == children[key],
            'EMPLOYER_INVENTORY_LINEAGE_CHANGED')
    _require(response['source_id'] == source['id']
        and response['parent_request_id'] == evidence['collection_request_id']
        and response['collection_lineage_sha256'] == children['lineage_sha256']
        and response['collection_readback_sha256'] == children['readback_sha256']
        and isinstance(response['observations'], list)
        and len(response['observations']) == len(children['children']) <= 2,
        'EMPLOYER_INVENTORY_LINEAGE_CHANGED')
    for row, child in zip(response['observations'], children['children']):
        _require(isinstance(row, dict) and set(row) == {'child', 'status', 'reason', 'proof'}
            and row['child'] == child, 'EMPLOYER_INVENTORY_LINEAGE_CHANGED')
        proof = row['proof']
        if proof is None:
            _require(row['status'] == 'UNAVAILABLE' and row['reason'] in {
                'NATIVE_INVENTORY_NOT_PERSISTED', 'CHILD_NOT_COMPLETED', 'NATIVE_SOURCE_NOT_ENROLLED',
                'DISCOVERY_OPERATION_UNAVAILABLE', 'DOMAIN_CORROBORATION_UNAVAILABLE',
                'BOOTSTRAP_RECEIPT_NOT_PERSISTED', 'OFFICIAL_DOMAIN_NOT_ACQUIRED',
                'DOMAIN_ACQUISITION_AUDIT_UNAVAILABLE'}, 'EMPLOYER_INVENTORY_INVALID')
            continue
        _check_seal(proof)
        _require(set(proof) == INVENTORY_FIELDS and type(proof['schema_version']) is int
            and proof['schema_version'] == 1 and child['state'] == 'COMPLETED', 'EMPLOYER_INVENTORY_INVALID')
        raw = {k: v for k, v in proof.items() if k not in {'audit_id', 'audit_sha256', 'receipt_sha256', 'sha256'}}
        _require(proof['receipt_sha256'] == _digest(raw), 'EMPLOYER_INVENTORY_INVALID')
        bindings = dict(company_id=child['company_id'], discovery_request_id=child['request_id'],
            request_revision=child['revision'], child_operation_id=child['operation_id'],
            child_operation_attempt=child['operation_attempt'], profile_id=child['profile_id'],
            profile_version=child['profile_version'], parent_operation_id=child['parent_operation_id'],
            parent_operation_attempt=child['parent_operation_attempt'],
            parent_request_id=evidence['collection_request_id'], pending_sha256=child['pending_sha256'])
        _require(all(type(proof[k]) is type(v) and proof[k] == v for k, v in bindings.items()),
            'EMPLOYER_INVENTORY_LINEAGE_CHANGED')
        enrolled = [item for item in children['enrolled_sources'] if item['request_id'] == child['request_id']]
        _require(len(enrolled) == 1, 'EMPLOYER_INVENTORY_LINEAGE_CHANGED')
        native = enrolled[0]
        _require(proof['collector_source_id'] == native['source_id']
            and proof['company_source_id'] == native['company_source_id']
            and proof['bootstrap_receipt_sha256'] == native['receipt_sha256']
            and proof['provider'] == native['policy']['provider'] == 'greenhouse'
            and proof['tenant'] == native['policy']['tenant']
            and proof['source_policy_sha256'] == _digest(source_policy_projection(native['policy']))
            and proof['endpoint'] == 'https://boards-api.greenhouse.io/v1/boards/' + proof['tenant'] + '/jobs',
            'EMPLOYER_INVENTORY_LINEAGE_CHANGED')
        _timestamp(proof['collection_started_at'])
        for key in ('enumerated_count', 'accepted_count', 'rejected_count'):
            _require(type(proof[key]) is int and proof[key] >= 0, 'EMPLOYER_INVENTORY_INVALID')
        _require(type(proof['provider_complete_inventory']) is bool
            and type(proof['inventory_complete']) is bool
            and proof['enumerated_count'] == proof['accepted_count'] + proof['rejected_count']
            and row['status'] == proof['status'] and row['reason'] == proof['reason'], 'EMPLOYER_INVENTORY_INVALID')
        if proof['status'] == 'UNAVAILABLE':
            _require(proof['reason'] == CAPACITY_REASON and proof['manifest'] is None
                and proof['manifest_sha256'] is None and proof['duplicate_count'] is None
                and proof['inventory_complete'] is False, 'EMPLOYER_INVENTORY_INVALID')
            continue
        manifest = proof['manifest']
        _require(proof['status'] == 'AVAILABLE' and proof['reason'] is None
            and isinstance(manifest, list) and len(manifest) <= MAX_ENTRIES
            and len(json.dumps(raw, sort_keys=True).encode()) <= MAX_BYTES
            and proof['manifest_sha256'] == _digest(manifest)
            and all(isinstance(item, dict) and set(item) == MANIFEST_FIELDS for item in manifest),
            'EMPLOYER_INVENTORY_INVALID')
        for item in manifest:
            for key in MANIFEST_FIELDS:
                _id(item[key])
        _require(len({item['posting_id'] for item in manifest}) == len(manifest)
            and len({item['candidate_id'] for item in manifest}) == len(manifest)
            and manifest == sorted(manifest, key=lambda item: item['posting_id'])
            and type(proof['duplicate_count']) is int
            and proof['duplicate_count'] == proof['accepted_count'] - len(manifest) >= 0
            and proof['inventory_complete'] == (proof['provider_complete_inventory']
                and proof['rejected_count'] == 0 and proof['duplicate_count'] == 0), 'EMPLOYER_INVENTORY_INVALID')


def _job_projection(job):
    # Do not retain profile facts, explanations, contact details, or provider HTML.
    postings = []
    for posting in job['postings']:
        snapshots = []
        for snapshot in posting.get('snapshots', []):
            snapshots.append({k: snapshot[k] for k in ('id', 'sha256')})
        postings.append({**{k: posting[k] for k in ('id', 'job_id', 'source', 'source_job_id')},
                         'raw_sha256': _digest(posting['raw']), 'snapshots': snapshots})
    return {**{k: job[k] for k in ('id', 'company_id', 'company', 'title')},
        'fictional': (job.get('attributes') or {}).get('fictional'),
        'match': {k: (job.get('match') or {}).get(k) for k in ('profile_id', 'profile_version', 'score')},
        'postings': postings}


def _posting(job, posting_id, snapshot_id, snapshot_sha256, endpoint, native_id=None):
    rows = [row for row in job['postings'] if row['id'] == posting_id]
    return (len(rows) == 1 and rows[0]['job_id'] == job['id'] and rows[0]['source'] == endpoint
        and rows[0]['raw_sha256'] == snapshot_sha256
        and (native_id is None or rows[0]['source_job_id'] == native_id)
        and sum(snapshot['id'] == snapshot_id and snapshot['sha256'] == snapshot_sha256
                for snapshot in rows[0]['snapshots']) == 1)


def _choose(initial, evidence, inventory, sources, jobs):
    if not inventory['observations']:
        return None
    domains = evidence['domain_proof_readback']['response']['observations']
    for row, domain_row in zip(inventory['observations'], domains):
        child, proof, domain = row['child'], row['proof'], domain_row['proof']
        if (not proof or not proof['inventory_complete'] or not domain
                or domain['verification_policy_sha256'] != initial['policy_sha256']):
            continue
        _require(domain_row['child'] == child, 'EMPLOYER_DOMAIN_LINEAGE_CHANGED')
        _require(_timestamp(initial['captured_at']) <= _timestamp(proof['collection_started_at']),
            'EMPLOYER_BASELINE_CHRONOLOGY_CHANGED')
        source_rows = [source for source in sources if source['id'] == proof['company_source_id']]
        _require(len(source_rows) == 1, 'EMPLOYER_NATIVE_SOURCE_CHANGED')
        source = source_rows[0]
        _require(source['company_id'] == child['company_id'] and source['profile_id'] == child['profile_id']
            and source['profile_company_associated'] is True and source['enabled'] is True
            and source['discovered_automatically'] is True and source['origin'] == 'AUTO_DISCOVERED'
            and source['provider'] == domain['native']['provider'] == proof['provider']
            and source['tenant'] == domain['native']['tenant'] == proof['tenant']
            and source['official_domain'] == domain['domain'], 'EMPLOYER_NATIVE_SOURCE_CHANGED')
        career = urlsplit(source['career_url'] or '')
        _require(career.scheme == 'https' and career.hostname == domain['domain']
            and career.username is None and career.password is None and career.port is None
            and not career.fragment, 'EMPLOYER_CAREER_URL_CHANGED')
        if source['company_id'] in initial['company_ids'] or source['id'] in initial['source_ids']:
            continue
        trigger = jobs.get(child['trigger_job_id'])
        if trigger is None:
            continue
        _require(trigger['fictional'] is False and trigger['company_id'] == child['company_id'] and _posting(trigger, child['posting_id'],
            child['snapshot_id'], child['snapshot_sha256'], evidence['endpoint']), 'EMPLOYER_TRIGGER_CHANGED')
        duplicate = [item for item in proof['manifest'] if item['canonical_job_id'] == trigger['id']
                     and item['source_job_id'] == domain['native']['job_id']]
        if len(duplicate) != 1:
            continue
        item = duplicate[0]
        _require(_posting(trigger, item['posting_id'], item['snapshot_id'], item['snapshot_sha256'],
            proof['endpoint'], item['source_job_id']), 'EMPLOYER_NATIVE_POSTING_CHANGED')
        for item in proof['manifest']:
            detail = jobs.get(item['canonical_job_id'])
            if detail is None or detail['id'] == trigger['id']:
                continue
            _require(detail['fictional'] is False and detail['company_id'] == child['company_id'] and _posting(detail, item['posting_id'],
                item['snapshot_id'], item['snapshot_sha256'], proof['endpoint'], item['source_job_id']),
                'EMPLOYER_NATIVE_POSTING_CHANGED')
            match = detail['match']
            _require(match['profile_id'] == child['profile_id'] and match['profile_version'] == child['profile_version'],
                'EMPLOYER_MATCH_PROFILE_CHANGED')
            if (type(match['score']) in (int, float) and 0 < match['score'] <= 100
                    and match['score'] >= initial['minimum_match_score']
                    and all(posting['source'] == proof['endpoint'] for posting in detail['postings'])):
                return dict(child=deepcopy(child), domain=deepcopy(domain), inventory=deepcopy(proof),
                    source=deepcopy(source), trigger_job=deepcopy(trigger), native_only_job=deepcopy(detail))
    return None


def capture(read, evidence, source, initial):
    """Capture under the caller's containment lease, before source cleanup."""
    _check_seal(initial)
    profile = read('GET', '/profiles/' + quote(initial['profile_id'], safe=''))
    policy = verification_policy_snapshot(profile)
    _require(profile['version'] == initial['profile_version'] and policy is not None
        and _digest(policy) == initial['policy_sha256'], 'EMPLOYER_PROFILE_CONSENT_CHANGED')
    prefix = '/profiles/' + quote(evidence['profile_id'], safe='') + '/sources/' + quote(source['id'], safe='')
    prefix += '/collections/' + quote(evidence['collection_operation_id'], safe='')
    inventory = read('GET', prefix + '/inventory-proofs', params={'attempt': evidence['collection_operation_attempt']})
    _inventory(inventory, evidence, source)
    sources = [{key: row[key] for key in SOURCE_FIELDS} for row in _sources(read, evidence['profile_id'])]
    ids = []
    for row in inventory['observations']:
        proof = row['proof']
        if not proof or not proof['inventory_complete']:
            continue
        ids.append(row['child']['trigger_job_id'])
        ids.extend(item['canonical_job_id'] for item in proof['manifest'])
    ids = list(dict.fromkeys(ids))[:MAX_DETAIL_READS]
    jobs = {}
    for identifier in ids:
        detail = read('GET', '/jobs/' + quote(identifier, safe=''), params={'profile_id': evidence['profile_id']})
        _require(detail.get('id') == identifier, 'EMPLOYER_JOB_CHANGED')
        jobs[identifier] = _job_projection(detail)
    selected = _choose(initial, evidence, inventory, sources, jobs)
    result = _sealed(dict(schema_version=1, status='COMPLETE' if selected else 'PARTIAL',
        reason=None if selected else 'SAME_EMPLOYER_EXAMPLES_NOT_OBSERVED', public_acceptance='NOT_RUN',
        execution_scope=evidence['execution_scope'], initial=deepcopy(initial),
        source_policy={key: source[key] for key in FINGERPRINT_FIELDS},
        inventory_response=inventory, source_rows=sources, jobs=jobs,
        detail_read_limit=MAX_DETAIL_READS, detail_reads=len(jobs), selected=selected))
    validate(result, evidence)
    return result


def validate(result, evidence):
    """Reconstruct joins during artifact replay; caller hashes still bind transport."""
    _check_seal(result)
    expected = {'schema_version', 'status', 'reason', 'public_acceptance', 'execution_scope', 'initial',
        'source_policy', 'inventory_response', 'source_rows', 'jobs', 'detail_read_limit', 'detail_reads', 'selected', 'sha256'}
    _require(set(result) == expected and type(result['schema_version']) is int and result['schema_version'] == 1
        and result['public_acceptance'] == 'NOT_RUN' and result['execution_scope'] == evidence['execution_scope']
        and result['execution_scope'] in {'CONTROLLED_TRANSPORT', 'LIVE_PUBLIC_READ_ONLY'}, 'EMPLOYER_EVIDENCE_CHANGED')
    initial = result['initial']
    _check_seal(initial)
    _require(set(initial) == {'schema_version', 'profile_id', 'profile_version', 'captured_at', 'scope',
        'policy_sha256', 'minimum_match_score', 'company_ids', 'source_ids', 'source_view_sha256', 'sha256'}
        and type(initial['schema_version']) is int and initial['schema_version'] == 1
        and type(initial['minimum_match_score']) in (int, float) and 0 <= initial['minimum_match_score'] <= 100
        and initial['scope'] == 'COMPLETE_PERSISTED_COMPANY_SOURCE_VIEW'
        and initial['profile_id'] == evidence['profile_id'] and initial['profile_version'] == evidence['profile_version'],
        'EMPLOYER_BASELINE_CHANGED')
    for key in ('company_ids', 'source_ids'):
        ids = initial[key]
        _require(isinstance(ids, list) and len(ids) <= MAX_SOURCE_ROWS
            and ids == sorted({_id(value) for value in ids}), 'EMPLOYER_BASELINE_CHANGED')
    _timestamp(initial['captured_at'])
    _require(_timestamp(initial['captured_at']) <= _timestamp(evidence['collection_started_at']),
        'EMPLOYER_BASELINE_CHRONOLOGY_CHANGED')
    source = result['source_policy']
    _require(set(source) == set(FINGERPRINT_FIELDS)
        and _digest(source) == evidence['source_policy_sha256'], 'EMPLOYER_SOURCE_POLICY_CHANGED')
    domain_evidence = deepcopy(evidence)
    saved = evidence['domain_proof_readback']
    if evidence['child_discovery']['children']:
        _require(saved['status'] == 'CAPTURED' and saved['response_sha256'] == saved['response']['sha256'],
            'EMPLOYER_DOMAIN_PROOF_MISSING')
        _observe_domain_proofs(lambda *args, **kwargs: deepcopy(saved['response']), domain_evidence, source)
    _inventory(result['inventory_response'], evidence, source)
    _require(type(result['detail_read_limit']) is int and result['detail_read_limit'] == MAX_DETAIL_READS
        and type(result['detail_reads']) is int and result['detail_reads'] == len(result['jobs']) <= MAX_DETAIL_READS,
        'EMPLOYER_DETAIL_BOUND_CHANGED')
    _require(isinstance(result['source_rows'], list) and len(result['source_rows']) <= MAX_SOURCE_ROWS
        and all(isinstance(row, dict) and set(row) == set(SOURCE_FIELDS) for row in result['source_rows'])
        and len({row['id'] for row in result['source_rows']}) == len(result['source_rows']), 'EMPLOYER_SOURCE_VIEW_INVALID')
    for identifier, job in result['jobs'].items():
        _require(set(job) == {'id', 'company_id', 'company', 'title', 'fictional', 'match', 'postings'}
            and job['id'] == identifier and set(job['match']) == {'profile_id', 'profile_version', 'score'}
            and isinstance(job['postings'], list) and len(job['postings']) <= 64, 'EMPLOYER_JOB_CHANGED')
        for posting in job['postings']:
            _require(set(posting) == {'id', 'job_id', 'source', 'source_job_id', 'raw_sha256', 'snapshots'}
                and posting['job_id'] == identifier and isinstance(posting['snapshots'], list)
                and len(posting['snapshots']) <= 128
                and all(set(row) == {'id', 'sha256'} for row in posting['snapshots']), 'EMPLOYER_SNAPSHOT_CHANGED')
    selected = _choose(initial, evidence, result['inventory_response'], result['source_rows'], result['jobs'])
    _require(result['selected'] == selected and result['status'] == ('COMPLETE' if selected else 'PARTIAL')
        and result['reason'] == (None if selected else 'SAME_EMPLOYER_EXAMPLES_NOT_OBSERVED'), 'EMPLOYER_CHAIN_CHANGED')


def browser_projection(result):
    """Expected assertions after the caller validates the complete source capture."""
    from cli.acceptance_employer import REQUIRED_CHECKS_BY_STEP
    selected = result['selected']
    _require(result['status'] == 'COMPLETE' and selected is not None, 'EMPLOYER_CHAIN_INCOMPLETE')
    return _sealed(dict(schema_version=1, capture_sha256=result['sha256'],
        company_source_id=selected['source']['id'],
        source_view_sha256=_digest(selected['source']),
        job_readbacks_sha256=_digest([selected['trigger_job'], selected['native_only_job']]),
        job_ids=sorted([selected['trigger_job']['id'], selected['native_only_job']['id']]),
        checks={next(iter(names)): True for names in REQUIRED_CHECKS_BY_STEP.values()}))


def observe_browser(page, read, result, evidence):
    """Exercise actual source and job views in the caller's GET/HEAD-only context.

    No provider links are followed. The enclosing browser observer owns egress
    denial, selected-application before/after equality and runtime bindings.
    """
    from playwright.sync_api import expect
    validate(result, evidence)
    selected = result['selected']
    _require(selected is not None, 'EMPLOYER_CHAIN_INCOMPLETE')
    profile_id = evidence['profile_id']
    profile = read('GET', '/profiles/' + quote(profile_id, safe=''))
    policy = verification_policy_snapshot(profile)
    _require(policy is not None and _digest(policy) == result['initial']['policy_sha256'],
        'EMPLOYER_PROFILE_CONSENT_CHANGED')
    source = selected['source']
    current = [row for row in _sources(read, profile_id) if row['id'] == source['id']]
    _require(len(current) == 1 and {key: current[0][key] for key in SOURCE_FIELDS} == source,
        'EMPLOYER_NATIVE_SOURCE_CHANGED')
    native = [row for row in read('GET', '/sources')['sources']
              if row['id'] == selected['inventory']['collector_source_id']]
    _require(len(native) == 1 and _digest(source_policy_projection(native[0])) ==
        selected['inventory']['source_policy_sha256'], 'EMPLOYER_NATIVE_POLICY_CHANGED')
    expect(page.get_by_label('Active profile', exact=True)).to_have_value(profile_id)
    page.get_by_role('navigation').get_by_role('button', name=re.compile(r'^Sources\b')).click()
    card = page.locator('.company-source-card[data-source-id="' + source['id'] + '"]')
    expect(card).to_be_visible()
    expect(card.get_by_role('heading', name=source['company_name'], exact=True)).to_be_visible()
    def fact(label):
        return card.locator('dt').filter(has_text=re.compile('^' + re.escape(label) + '$')).locator('..').locator('dd')
    expect(fact('Domain')).to_have_text(source['official_domain'])
    expect(fact('Career Site').get_by_role('link')).to_have_attribute('href', source['career_url'])
    expect(fact('ATS')).to_have_text(source['provider'] + ' / ' + source['tenant'])
    expect(fact('Source Type')).to_contain_text(source['collection_strategy'])
    expect(fact('Source Type')).to_contain_text('AUTO_DISCOVERED')
    expect(fact('Status')).to_have_text('ENABLED')
    expect(fact('Jobs Found')).to_have_text(str(len({item['canonical_job_id'] for item in selected['inventory']['manifest']})))
    page.get_by_role('navigation').get_by_role('button', name=re.compile(r'^Jobs\b')).click()
    page.get_by_label('Saved search profile', exact=True).select_option('')
    page.get_by_label('Search jobs', exact=True).fill('')
    origin = urlsplit(page.url).netloc
    for expected in (selected['trigger_job'], selected['native_only_job']):
        path = '/jobs/' + quote(expected['id'], safe='')
        current_job = read('GET', path, params={'profile_id': profile_id})
        _require(_job_projection(current_job) == expected, 'EMPLOYER_JOB_CHANGED')
        page.get_by_label('Job title', exact=True).fill(expected['title'])
        job_card = page.get_by_role('button', name=expected['company'] + ' ' + expected['title'], exact=True)
        expect(job_card).to_have_count(1)
        expect(job_card).to_be_visible()
        def response_matches(response):
            url = urlsplit(response.url)
            return (response.request.method == 'GET' and url.netloc == origin and url.path == path
                and parse_qs(url.query).get('profile_id') == [profile_id])
        with page.expect_response(response_matches) as selected_response:
            job_card.click()
        response = selected_response.value
        _require(response.status == 200 and _job_projection(response.json()) == expected,
            'EMPLOYER_SELECTED_JOB_CHANGED')
        detail = page.locator('section.job-detail')
        expect(detail.locator('h2').first).to_have_text(expected['title'])
        expect(detail.locator('.eyebrow').first).to_have_text(expected['company'])
        for posting in expected['postings']:
            provenance = detail.locator('.provenance').filter(has_text=posting['source'])
            expect(provenance.first).to_be_visible()
            expect(provenance.first).to_contain_text('SHA256 ' + posting['raw_sha256'])
    return browser_projection(result)
