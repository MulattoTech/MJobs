"""Observe the actual first-run wizard without inventing a setup-complete API.

Start before the journey's resume import; observe after its saved fixture search.
All mutations use visible React controls. API reads bind the observed profile,
configuration and search lineage; setup acknowledgements remain browser-local.
The caller owns a fresh isolated FIXTURE stack and the Playwright context.
"""
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import quote, urlsplit

from playwright.sync_api import expect


CHECK = 'first_run_wizard_completed_with_persisted_configuration'
STEPS = (
    ('create-profile', 'Create profile'),
    ('upload-resume', 'Upload resume'),
    ('review-extracted-facts', 'Review extracted facts'),
    ('configure-desired-jobs', 'Configure desired jobs'),
    ('configure-location', 'Configure location and remote preferences'),
    ('configure-application-preferences', 'Configure application preferences'),
    ('configure-ai', 'Configure AI'),
    ('enable-sources', 'Enable sources'),
    ('choose-automation-level', 'Choose automation level'),
    ('run-first-search', 'Run first search'),
)
CHOICES = {
    'configure-ai': 'Keep AI disabled',
    'enable-sources': 'Skip source setup for now',
    'choose-automation-level': 'Keep manual review only',
}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _read(page, origin, path):
    response = page.request.get(origin + path, max_redirects=0)
    _require(response.ok, 'FIRST_RUN_API_READ_FAILED')
    return response.json()


def _preflight(page, base_url):
    url, current = urlsplit(base_url), urlsplit(page.url)
    _require(url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}
        and url.username is None and url.password is None and not url.query and not url.fragment
        and url.path in ('', '/') and current.scheme == url.scheme and current.netloc == url.netloc,
        'FIRST_RUN_LOOPBACK_PAGE_REQUIRED')
    origin = base_url.rstrip('/')
    settings = _read(page, origin, '/settings')
    _require(settings.get('operation_mode') == 'FIXTURE'
        and settings.get('external_submission_enabled') is False, 'FIRST_RUN_FIXTURE_REQUIRED')
    return origin


def _wizard_steps(page):
    wizard = page.get_by_role('region', name='First-run setup', exact=True)
    expect(wizard).to_be_visible()
    rows = wizard.get_by_role('listitem')
    expect(rows).to_have_count(len(STEPS))
    observed = []
    for index, (identifier, label) in enumerate(STEPS):
        row = rows.nth(index)
        expect(row.locator('strong')).to_have_text(f'{index + 1}. {label}')
        state = row.locator('span.status').inner_text()
        _require(state in {'Complete', 'Needs attention'}, 'FIRST_RUN_STEP_STATE_INVALID')
        observed.append(dict(id=identifier, complete=state == 'Complete'))
    return observed


def _acknowledgements(page, profile_id):
    raw = page.evaluate('key => localStorage.getItem(key)',
        'mjobs-first-run-acknowledgements:' + profile_id)
    values = json.loads(raw or '[]')
    _require(isinstance(values, list) and all(isinstance(item, str) for item in values),
             'FIRST_RUN_ACKNOWLEDGEMENTS_INVALID')
    return sorted(values)


def _configuration(page, origin, profile_id):
    encoded = quote(profile_id, safe='')
    profile = _read(page, origin, '/profiles/' + encoded)
    ai = _read(page, origin, '/profiles/' + encoded + '/ai-task-settings')
    _require(profile['id'] == profile_id and ai['profile_id'] == profile_id,
             'FIRST_RUN_PROFILE_CHANGED')
    preferences = profile.get('preferences') or {}
    acknowledgements = _acknowledgements(page, profile_id)
    _require(set(CHOICES).issubset(acknowledgements), 'FIRST_RUN_SETUP_CHOICES_CHANGED')
    _require(preferences.get('work_arrangements') == ['REMOTE']
        and preferences.get('application_route_preference', 'PREFER_VERIFIED') == 'PREFER_VERIFIED'
        and ai['tasks'] and all(item['provider'] == 'disabled' for item in ai['tasks'].values()),
        'FIRST_RUN_CONFIGURATION_CHANGED')
    return dict(profile_id=profile_id, profile_version=profile['version'],
        profile_sha256=_digest(profile), ai_settings_sha256=_digest(ai),
        work_arrangements=preferences['work_arrangements'], application_route_preference='PREFER_VERIFIED',
        ai_providers_disabled=True, external_submission_enabled=False,
        browser_acknowledgements=acknowledgements,
        acknowledgement_persistence='CURRENT_BROWSER_LOCAL_STORAGE')


def begin_first_run(page, base_url, *, acceptance_run_id):
    """Start only on an empty fixture workspace; return its UI-created identity.

    This deliberately leaves resume review and saved searches to the existing
    connected journey. Its result is PARTIAL until observe_first_run completes.
    An existing workspace is NOT_RUN and is never reset or relabelled as fresh.
    """
    _require(isinstance(acceptance_run_id, str) and bool(acceptance_run_id), 'FIRST_RUN_ID_REQUIRED')
    origin = _preflight(page, base_url)
    receipt = dict(schema_version=1, acceptance_run_id=acceptance_run_id,
        timestamp=datetime.now(timezone.utc).isoformat(), result='NOT_RUN', first_run_started=False,
        checks=[dict(name=CHECK, passed=False)])
    profiles = _read(page, origin, '/profiles')['profiles']
    sources = _read(page, origin, '/sources')['sources']
    searches = _read(page, origin, '/search-profiles')['search_profiles']
    previous_setup = page.evaluate("() => Object.keys(localStorage).some(key => "
        "key.startsWith('mjobs-first-run-'))")
    if profiles or sources or searches or previous_setup:
        return dict(receipt, reason='FIRST_RUN_WORKSPACE_NOT_FRESH')
    initial_steps = _wizard_steps(page)
    _require(all(not row['complete'] for row in initial_steps), 'FIRST_RUN_NOT_INCOMPLETE')
    wizard = page.get_by_role('region', name='First-run setup', exact=True)
    wizard.get_by_role('button', name='Open profile', exact=True).click()
    page.get_by_label('Full name', exact=True).fill('Synthetic Onboarding Applicant')
    page.get_by_label('Email', exact=True).fill('onboarding@example.invalid')
    page.locator('label').filter(has_text='Work arrangements').locator('select').select_option('REMOTE')
    # Select the intended value explicitly; the server also supplies it as the
    # default, so the wizard's optional "Keep current default" vanishes on save.
    page.get_by_label('Ask me to choose a route for each job', exact=True).check()
    page.get_by_label('Prefer a verified employer or ATS route when evidence supports it', exact=True).check()
    with page.expect_response(lambda response: response.request.method == 'PUT'
            and response.url == origin + '/profiles') as saved:
        page.get_by_role('button', name='Save profile', exact=True).click()
    _require(saved.value.status == 200, 'FIRST_RUN_PROFILE_SAVE_FAILED')
    profile_id = saved.value.json()['id']
    expect(page.get_by_label('Active profile', exact=True)).to_have_value(profile_id)
    expect(page.get_by_role('button', name='Save profile', exact=True)).to_be_enabled()
    for label in CHOICES.values():
        wizard.get_by_role('button', name=label, exact=True).click()
    configuration = _configuration(page, origin, profile_id)
    _require(len(_read(page, origin, '/profiles')['profiles']) == 1, 'FIRST_RUN_PROFILE_CHANGED')
    return dict(receipt, result='PARTIAL', first_run_started=True, profile_id=profile_id,
        profile_version=configuration['profile_version'], initial_steps=initial_steps,
        initial_configuration=configuration,
        setup_choices=dict(work_arrangements=['REMOTE'], application_route='PREFER_VERIFIED_SELECTED',
            ai='KEEP_DISABLED', source_setup='EXPLICITLY_DEFERRED', automation='MANUAL_REVIEW_ONLY'),
        reason='FIRST_RUN_AWAITING_RESUME_REVIEW_AND_SAVED_SEARCH')


def observe_first_run(page, base_url, initial, *, search_profile_id=None, operation_id=None, request_id=None):
    """Reload and bind all ten visible steps to this run's exact saved identity.

    Acknowledgements are browser-local, not durable server setup flags. Pending
    facts remain pending: this observer never approves facts or changes data.
    The optional IDs must name the same completed fixture discovery, or no PASS
    is emitted even when an unrelated completed history row makes the UI green.
    """
    receipt = dict(schema_version=1, result='FAIL', acceptance_run_id=initial.get('acceptance_run_id'),
        profile_id=initial.get('profile_id'), timestamp=datetime.now(timezone.utc).isoformat(),
        checks=[dict(name=CHECK, passed=False)])
    try:
        origin = _preflight(page, base_url)
        _require(initial.get('first_run_started') is True and initial.get('result') == 'PARTIAL'
            and initial.get('initial_steps') == [dict(id=identifier, complete=False) for identifier, _ in STEPS],
            'FIRST_RUN_INITIAL_OBSERVATION_REQUIRED')
        profile_id = initial['profile_id']
        _require(page.get_by_label('Active profile', exact=True).input_value() == profile_id,
                 'FIRST_RUN_PROFILE_CHANGED')
        page.reload(wait_until='domcontentloaded')
        expect(page.get_by_label('Active profile', exact=True)).to_have_value(profile_id)
        expect(page.get_by_label('Active profile', exact=True)).to_be_enabled()
        configuration = _configuration(page, origin, profile_id)
        history = _read(page, origin, '/profiles/' + quote(profile_id, safe='') + '/search-history?limit=100')
        _require(history['profile_id'] == profile_id, 'FIRST_RUN_PROFILE_CHANGED')
        wizard = page.get_by_role('region', name='First-run setup', exact=True)
        if any(item.get('status') == 'COMPLETED' for item in history['items']):
            expect(wizard.get_by_role('listitem').nth(9).get_by_text('Complete', exact=True)).to_be_visible()
        steps = _wizard_steps(page)
        pending = [row['id'] for row in steps if not row['complete']]
        receipt.update(configuration=configuration, steps=steps, completed_steps=len(STEPS) - len(pending),
            pending_steps=pending, reloaded=True, initial_steps=initial['initial_steps'])
        if not all((search_profile_id, operation_id, request_id)):
            return dict(receipt, result='PARTIAL', reason='FIRST_RUN_SEARCH_LINEAGE_NOT_OBSERVED')
        search = _read(page, origin, '/search-profiles/' + quote(search_profile_id, safe=''))
        operation = _read(page, origin, '/operations/' + quote(operation_id, safe=''))
        params = operation.get('params') or {}
        _require(search.get('id') == search_profile_id and search.get('profile_id') == profile_id
            and search.get('enabled') is True and search.get('criteria', {}).get('include_titles')
            and operation.get('id') == operation_id and operation.get('kind') == 'DISCOVER'
            and operation.get('entity_id') == profile_id and operation.get('state') == 'COMPLETED'
            and params.get('request_id') == request_id
            and params.get('search_profile_id') == search_profile_id
            and params.get('profile_version') == configuration['profile_version'],
            'FIRST_RUN_SEARCH_LINEAGE_MISMATCH')
        history_id = operation_id + ':' + str(operation['attempt_count'])
        rows = [item for item in history['items'] if item.get('id') == history_id]
        _require(len(rows) == 1 and rows[0].get('profile_id') == profile_id
            and rows[0].get('status') == 'COMPLETED' and rows[0].get('source') == 'mock-fixture'
            and rows[0].get('filters') == search['criteria'], 'FIRST_RUN_SEARCH_LINEAGE_MISMATCH')
        receipt.update(search_profile_id=search_profile_id, discovery_operation_id=operation_id,
            discovery_request_id=request_id, search_sha256=_digest(search), operation_sha256=_digest(operation),
            search_history=rows[0], search_history_sha256=_digest(rows[0]))
        if pending:
            return dict(receipt, result='PARTIAL', reason='FIRST_RUN_STEPS_INCOMPLETE')
        receipt.update(result='PASS', checks=[dict(name=CHECK, passed=True)])
        return receipt
    except ValueError as error:
        return dict(receipt, reason=str(error))
