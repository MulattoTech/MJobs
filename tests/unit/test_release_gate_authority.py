"""Release readiness must follow canonical acceptance, not a stale derived index."""

from tools.ci.release_gate import acceptance_authority_errors, unmet


def fixtures():
    capability_rows = [
        {'requirement_id': f'MJ-REQ-{number:03}', 'result': 'PASS',
         'priority': 'P0' if number == 0 else 'P2'}
        for number in range(366)
    ]
    reference_ids = [f'MJ-REF-{number:03}' for number in range(1, 60)]
    reference = {
        'canonical_ids': reference_ids,
        'result_by_id': {identifier: {'result': 'PASS'} for identifier in reference_ids},
        'commercial_features': {},
    }
    index_rows = [
        {'id': row['requirement_id'], 'priority': row['priority'], 'status': row['result']}
        for row in capability_rows
    ] + [
        {'id': identifier, 'priority': 'P3', 'status': 'PASS'}
        for identifier in reference_ids
    ]
    return {'requirements': index_rows}, {'requirements': capability_rows}, reference


def test_release_gate_accepts_index_synchronized_with_both_authorities():
    index, capability, reference = fixtures()

    assert acceptance_authority_errors(index, capability, reference) == []


def test_release_gate_rejects_false_p0_pass_in_derived_index():
    index, capability, reference = fixtures()
    index['requirements'][0]['status'] = 'PASS'
    capability['requirements'][0]['result'] = 'PARTIAL'

    errors = acceptance_authority_errors(index, capability, reference)

    assert any('MJ-REQ-000' in error and 'status differs' in error for error in errors)
    assert unmet(index['requirements'], 'p0p1') == []
    assert capability['requirements'][0]['result'] == 'PARTIAL'


def test_release_gate_rejects_priority_drift_that_would_hide_a_p0():
    index, capability, reference = fixtures()
    index['requirements'][0]['priority'] = 'P4'

    errors = acceptance_authority_errors(index, capability, reference)

    assert any('MJ-REQ-000' in error and 'priority differs' in error for error in errors)
    assert unmet(index['requirements'], 'p0p1') == ['No acceptance criteria were selected']


def test_release_gate_rejects_missing_canonical_rows():
    index, capability, reference = fixtures()
    index['requirements'].pop()

    errors = acceptance_authority_errors(index, capability, reference)

    assert errors == ['Requirement index coverage differs from canonical acceptance matrices']
