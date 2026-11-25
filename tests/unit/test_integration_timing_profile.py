"""Negative controls for integration timing evidence and scheduling."""

import pytest

from tools.ci.integration_timing_profile import (
    blend_timings,
    complete_profile,
    report_timings,
)

FILES = ['tests/integration/test_a.py', 'tests/integration/test_b.py']


def make_report(tmp_path, name, cases):
    path = tmp_path / name
    path.write_text(f'<testsuite>{cases}</testsuite>', encoding='utf-8')
    return path


def test_complete_profile_aggregates_by_file(tmp_path):
    a = make_report(tmp_path, 'a.xml',
                    '<testcase classname="tests.integration.test_a.C" name="a" time="1.25"/>'
                    '<testcase classname="tests.integration.test_a" name="b" time="2.75"/>')
    b = make_report(tmp_path, 'b.xml',
                    '<testcase classname="tests.integration.test_b" name="c" time="1.0"><skipped/></testcase>')
    assert complete_profile(FILES, [a, b]) == dict(zip(FILES, [4.0, 1.0], strict=True))


def test_complete_profile_rejects_missing_and_duplicate_files(tmp_path):
    a = make_report(tmp_path, 'a.xml',
                    '<testcase classname="tests.integration.test_a" name="a" time="1"/>')
    with pytest.raises(ValueError, match='Missing test files'):
        complete_profile(FILES, [a])
    with pytest.raises(ValueError, match='multiple shard reports'):
        complete_profile(FILES, [a, a])


@pytest.mark.parametrize(('cases', 'error'), [
    ('', 'Empty test report'),
    ('<testcase classname="unknown" name="test_x" time="1"/>', 'Unmapped testcase'),
    ('<testcase classname="tests.integration.test_a" name="test_x" time="nan"/>', 'Invalid testcase duration'),
    ('<testcase classname="tests.integration.test_a" name="test_x" time="-1"/>', 'Invalid testcase duration'),
    ('<testcase classname="tests.integration.test_a" name="test_x" time="1"><failure/></testcase>', 'Failed testcase'),
    ('<testcase classname="tests.integration.test_a" name="test_x" time="1"><error/></testcase>', 'Failed testcase'),
])
def test_report_timings_rejects_invalid_evidence(tmp_path, cases, error):
    report = make_report(tmp_path, 'bad.xml', cases)
    with pytest.raises(ValueError, match=error):
        report_timings(report, FILES)

def test_blend_timings_weights_recent_data_and_keeps_current_inventory():
    latest = {'a': 12.0, 'b': 6.0, 'new': 4.0}
    previous = {'a': 6.0, 'b': float('nan'), 'removed': 20.0}
    assert blend_timings(latest, previous) == {'a': 10.0, 'b': 6.0, 'new': 4.0}


@pytest.mark.parametrize('weight', [0, -1, 2, float('nan'), float('inf')])
def test_blend_timings_rejects_invalid_weights(weight):
    with pytest.raises(ValueError, match='Latest weight'):
        blend_timings({'a': 1.0}, {}, weight)
