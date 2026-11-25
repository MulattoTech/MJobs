"""Benchmark inventory comparisons reject false-positive speed improvements."""
import pytest

from tools.ci.integration_benchmark import compare_inventories, read_inventory


def test_parity_accepts_additional_pass_without_removing_originals():
    a, b, c = ('module', 'a'), ('module', 'b'), ('module', 'c')
    result = compare_inventories({a: 'passed', b: 'skipped'},
                                 {a: 'passed', b: 'skipped', c: 'passed'})
    assert result['inventory_parity_pass']
    assert result['added'] == [c]


@pytest.mark.parametrize('replacement', [None, 'skipped', 'failure', 'error'])
def test_parity_rejects_regressions(replacement):
    key = ('module', 'test_a')
    result = compare_inventories({key: 'passed'}, {} if replacement is None else {key: replacement})
    assert not result['inventory_parity_pass']


def test_duplicate_identity_across_shards_rejected(tmp_path):
    for n in range(2):
        (tmp_path / f'integration-{n}.xml').write_text(
            '<testsuite><testcase classname="m" name="t"/></testsuite>', encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate test identity'):
        read_inventory(tmp_path)


def test_empty_report_rejected(tmp_path):
    (tmp_path / 'integration-0.xml').write_text('<testsuite/>', encoding='utf-8')
    with pytest.raises(ValueError, match='Empty report'):
        read_inventory(tmp_path)


def test_missing_shard_report_rejected(tmp_path):
    (tmp_path / 'integration-0.xml').write_text(
        '<testsuite><testcase classname="m" name="t"/></testsuite>', encoding='utf-8')
    with pytest.raises(ValueError, match='Missing, unexpected or duplicate'):
        read_inventory(tmp_path, 2)
