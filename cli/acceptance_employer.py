"""Observe the connected fixture employer flywheel for MJ-REQ-292 steps 23-31.

Capture before the first UI collection; observe after that actual operation.
This module never dispatches discovery, edits profiles/sources, or contacts
providers. Opening jobs uses the ordinary UI and records its view audit. The
caller owns the browser, collection and evidence envelope. The fixture executes
employer discovery inside DISCOVER, not as a separate COMPANY_DISCOVER task.
An unverified fixture website never earns official-domain acceptance.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import quote, urlsplit

from playwright.sync_api import expect


REQUIRED_CHECKS_BY_STEP = {
    23: frozenset({'previously_unknown_employer_observed'}),
    24: frozenset({'employer_discovery_ran_automatically'}),
    25: frozenset({'official_employer_domain_verified'}),
    26: frozenset({'employer_careers_page_found'}),
    27: frozenset({'employer_ats_tenant_identified'}),
    28: frozenset({'employer_source_enrolled_automatically'}),
    29: frozenset({'employer_openings_enumerated'}),
    30: frozenset({'employer_only_opening_observed'}),
    31: frozenset({'employer_duplicate_sources_collapsed'}),
}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _preflight(page, request, profile_id):
    _require(isinstance(profile_id, str) and _ID.fullmatch(profile_id), 'EMPLOYER_PROFILE_INVALID')
    url = urlsplit(page.url)
    _require(url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost', '::1'}
        and url.username is None and url.password is None, 'EMPLOYER_LOCAL_PAGE_REQUIRED')
    settings = request('/settings')
    _require(settings.get('operation_mode') == 'FIXTURE'
        and settings.get('external_submission_enabled') is False, 'EMPLOYER_FIXTURE_REQUIRED')
    expect(page.get_by_label('Active profile', exact=True)).to_have_value(profile_id)
    profile = request('/profiles/' + quote(profile_id, safe=''))
    _require(profile.get('id') == profile_id, 'EMPLOYER_PROFILE_CHANGED')
    return profile


def begin_employer_observation(page, request, profile_id):
    """Read-only baseline before the first Sources collection in this journey."""
    profile = _preflight(page, request, profile_id)
    sources = request('/company-sources/view?profile_id=' + quote(profile_id, safe=''))['company_sources']
    jobs = request('/jobs?profile_id=' + quote(profile_id, safe=''))['jobs']
    return {'profile_id': profile_id, 'profile_version': profile['version'],
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'company_ids': sorted({row['company_id'] for row in sources} | {row['company_id'] for row in jobs}),
        'source_ids': sorted(row['id'] for row in sources),
        'job_ids': sorted(row['id'] for row in jobs),
        'snapshot_sha256': _digest({'sources': sources, 'jobs': jobs})}


def observe_employer_journey(page, request, profile_id, navigate, observe, initial,
                             discovery_receipt, record_observed_jobs=None):
    """Emit nine ordered CONTROLLED_FIXTURE rows using the existing observe callback.

    ``discovery_receipt`` binds profile_id/search_profile_id/operation_id/request_id
    from the FIRST UI collection, even if the caller subsequently collects again.
    The returned source/job identifiers are supporting evidence, not a new selected
    application. Missing proof stays PARTIAL; invalid lineage fails every row.
    """
    passed = {step: False for step in REQUIRED_CHECKS_BY_STEP}
    continuity = {'profile_id': profile_id}
    evidence = {'result': 'FAIL', 'profile_id': profile_id,
        'evidence_kind': 'CONTROLLED_FIXTURE', 'observed_job_ids': []}
    reason = None
    try:
        profile = _preflight(page, request, profile_id)
        receipt = discovery_receipt
        _require(isinstance(initial, dict) and initial.get('profile_id') == profile_id
            and initial.get('profile_version') == profile['version']
            and isinstance(receipt, dict) and receipt.get('profile_id') == profile_id,
            'EMPLOYER_LINEAGE_MISMATCH')
        for key in ('operation_id', 'request_id', 'search_profile_id'):
            _require(isinstance(receipt.get(key), str) and _ID.fullmatch(receipt[key]),
                'EMPLOYER_LINEAGE_MISMATCH')
        operation = request('/operations/' + quote(receipt['operation_id'], safe=''))
        params = operation.get('params') or {}
        _require(operation.get('id') == receipt['operation_id']
            and operation.get('entity_id') == profile_id and operation.get('kind') == 'DISCOVER'
            and operation.get('state') == 'COMPLETED'
            and params.get('request_id') == receipt['request_id']
            and params.get('search_profile_id') == receipt['search_profile_id']
            and params.get('profile_version') == initial['profile_version']
            and _time(operation['last_attempt_at']) >= _time(initial['captured_at']),
            'EMPLOYER_LINEAGE_MISMATCH')
        search = next((row for row in request('/search-profiles?profile_id=' + quote(profile_id, safe=''))['search_profiles']
            if row['id'] == receipt['search_profile_id']), None)
        _require(search is not None and search.get('profile_id') == profile_id,
            'EMPLOYER_SEARCH_MISMATCH')
        history = request('/profiles/' + quote(profile_id, safe='') + '/search-history')
        history_row = next((row for row in history.get('items', [])
            if row.get('id') == operation['id'] + ':' + str(operation['attempt_count'])), None)
        _require(history.get('profile_id') == profile_id and history_row is not None
            and history_row.get('profile_id') == profile_id
            and history_row.get('status') == 'COMPLETED'
            and history_row.get('source') == 'mock-fixture', 'EMPLOYER_HISTORY_MISMATCH')
        sources = request('/company-sources/view?profile_id=' + quote(profile_id, safe=''))['company_sources']
        candidates = [row for row in sources if row.get('company_name') == 'Example Systems Inc.'
            and row.get('provider') == 'mock-ats' and row.get('tenant') == 'example'
            and row.get('collection_strategy') == 'FIXTURE'
            and row.get('profile_id') == profile_id and row.get('profile_company_associated') is True]
        _require(len(candidates) == 1, 'EMPLOYER_SOURCE_UNAVAILABLE')
        source = candidates[0]
        jobs = request('/jobs?profile_id=' + quote(profile_id, safe=''))['jobs']
        employer_jobs = [row for row in jobs if row.get('company_id') == source['company_id']]
        _require(len(employer_jobs) == 2 and {row.get('requisition') for row in employer_jobs} == {'A', 'B'},
            'EMPLOYER_INVENTORY_MISMATCH')
        details = {row['requisition']: request('/jobs/' + quote(row['id'], safe='')
            + '?profile_id=' + quote(profile_id, safe='')) for row in employer_jobs}
        for row in employer_jobs:
            detail = details[row['requisition']]
            _require(detail.get('id') == row['id'] and detail.get('company_id') == source['company_id']
                and detail.get('attributes', {}).get('fictional') is True, 'EMPLOYER_JOB_MISMATCH')
        job_a, job_b = details['A'], details['B']
        continuity.update(operation_id=operation['id'], request_id=receipt['request_id'], job_id=job_b['id'])
        events = [event for event in source.get('provenance', {}).get('events', [])
            if event.get('action') == 'COMPANY_SOURCE_DISCOVERED'
            and event.get('details', {}).get('company_source_id') == source['id']
            and _time(operation['last_attempt_at']) <= _time(event['timestamp']) <= _time(operation['updated_at'])]
        event = events[0] if len(events) == 1 else None
        event_details = event.get('details', {}) if event else {}
        new = (source['company_id'] not in initial['company_ids']
            and source['id'] not in initial['source_ids']
            and all(row['id'] not in initial['job_ids'] for row in employer_jobs))
        navigate('Sources')
        card = page.locator('.company-source-card[data-source-id="' + source['id'] + '"]')
        expect(card).to_be_visible()
        expect(card.get_by_role('heading', name=source['company_name'], exact=True)).to_be_visible()
        def fact(label):
            return card.locator('dt').filter(has_text=re.compile('^' + re.escape(label) + '$')).locator('..').locator('dd')
        expect(fact('ATS')).to_have_text('mock-ats / example')
        expect(card.get_by_text('Local fixture source.', exact=True)).to_be_visible()
        passed[23] = new
        passed[24] = (new and event is not None and source.get('discovered_automatically') is True
            and source.get('origin') == 'AUTO_DISCOVERED')
        domain = source.get('official_domain')
        expect(fact('Domain')).to_have_text(domain or 'Not verified')
        # The current fixture has no independently verified official domain.
        # A candidate homepage or confidence alone is never a substitute.
        passed[25] = False
        career = source.get('career_url') or ''
        parsed = urlsplit(career)
        career_bound = (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', 'mock'}
            and parsed.path == '/company/careers' and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment and event_details.get('careers_url') == career)
        if career_bound:
            expect(fact('Career Site').get_by_role('link')).to_have_attribute('href', career)
        passed[26] = career_bound
        passed[27] = (event_details.get('provider') == 'mock-ats' and event_details.get('tenant') == 'example'
            and 'fixture ATS tenant metadata' in source.get('provenance', {}).get('tenant_evidence', []))
        passed[28] = passed[24] and source.get('enabled') is True
        if passed[28]:
            expect(fact('Source Type')).to_contain_text('FIXTURE')
            expect(fact('Source Type')).to_contain_text('AUTO_DISCOVERED')
            expect(fact('Status')).to_have_text('ENABLED')
        endpoint = event_details.get('endpoint')
        expected_endpoint = career.rsplit('/company/careers', 1)[0] + '/ats/example/jobs'
        postings_a = job_a.get('postings', [])
        postings_b = job_b.get('postings', [])
        ids_a = sorted(row.get('source_job_id', '') for row in postings_a)
        ids_b = sorted(row.get('source_job_id', '') for row in postings_b)
        direct = [row for detail in details.values() for row in detail.get('postings', [])
            if row.get('source') == endpoint]
        passed[29] = (type(event_details.get('jobs_enumerated')) is int
            and event_details['jobs_enumerated'] == 2 and endpoint == expected_endpoint
            and len(direct) == 2
            and {row.get('source_job_id') for row in direct} == {'employer-A', 'employer-B'})
        navigate('Jobs')
        page.get_by_label('Saved search profile', exact=True).select_option('')
        page.get_by_label('Search jobs', exact=True).fill('')
        page.get_by_label('Job title', exact=True).fill('')
        for detail in (job_b, job_a):
            job_card = page.get_by_role('button', name=source['company_name'] + ' ' + detail['title'], exact=True)
            expect(job_card).to_be_visible()
            job_card.click()
            panel = page.locator('section.job-detail')
            expect(panel.get_by_role('heading', name=detail['title'], exact=True)).to_be_visible()
            expect(panel.locator('.provenance')).to_have_count(len(detail['postings']))
        match_score = (job_b.get('match') or {}).get('score')
        passed[30] = (passed[29] and job_b['title'] == 'Data Engineer' and ids_b == ['employer-B']
            and type(match_score) in (int, float) and 0 < match_score <= 100
            and all(row.get('source') == endpoint for row in postings_b))
        passed[31] = (ids_a == ['board-A', 'board-A-syndicated', 'employer-A']
            and len({row['id'] for row in postings_a}) == 3
            and all(row.get('job_id') == job_a['id'] for row in postings_a)
            and sum(row.get('source') == 'mock-board' for row in postings_a) == 2
            and sum(row.get('source') == endpoint for row in postings_a) == 1)
        observed_ids = sorted(row['id'] for row in employer_jobs)
        if record_observed_jobs:
            record_observed_jobs(observed_ids)
        evidence.update(result='PARTIAL', observed_job_ids=observed_ids,
            employer_discovery_lineage={'profile_id': profile_id, 'operation_id': operation['id'],
                'request_id': receipt['request_id'], 'job_ids': observed_ids},
            discovery_operation_id=operation['id'], discovery_request_id=receipt['request_id'],
            search_profile_id=receipt['search_profile_id'], company_id=source['company_id'],
            source={key: source.get(key) for key in ('id', 'company_id', 'provider', 'tenant',
                'collection_strategy', 'official_domain', 'career_url', 'enabled', 'origin')},
            source_event_id=event.get('id') if event else None,
            source_snapshot_sha256=_digest(source), job_snapshot_sha256=_digest(details),
            initial_snapshot_sha256=initial['snapshot_sha256'],
            employer_only_posting_ids=ids_b, duplicate_posting_ids=ids_a)
    except (ValueError, KeyError, TypeError, AssertionError) as error:
        passed = {step: False for step in REQUIRED_CHECKS_BY_STEP}
        reason = str(error) if isinstance(error, ValueError) and str(error).startswith('EMPLOYER_') else 'EMPLOYER_OBSERVATION_FAILED'
        evidence.update(result='FAIL', reason=reason)
    for step, names in REQUIRED_CHECKS_BY_STEP.items():
        status = 'FAIL' if reason else 'PASS' if passed[step] else 'PARTIAL'
        result = ('Connected fixture employer evidence verified.' if passed[step]
            else 'Official employer domain remains unverified in this fixture.' if step == 25 and not reason
            else 'Connected employer proof is missing or does not match the observed lineage.')
        row_continuity = dict(continuity)
        if step == 31 and evidence['observed_job_ids']:
            row_continuity['job_id'] = job_a['id']
        observe(step, status, result, evidence_kind='CONTROLLED_FIXTURE', continuity=row_continuity,
            checks=[{'name': name, 'passed': passed[step]} for name in sorted(names)])
    return evidence
