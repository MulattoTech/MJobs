"""Deterministic contracts; these do not certify connected Compose persistence."""
from pathlib import Path
import re

from cli import local_backup
import copy
import hashlib
import json

import pytest


def test_documented_stop_quiesces_every_backup_writer():
    text = (Path(__file__).resolve().parents[2] / 'docs/runbooks/LOCAL_BACKUP.md').read_text()
    services = re.search(r'^docker compose -p \$project stop (.+)$', text, re.M)[1].split()
    states = dict.fromkeys(local_backup.WRITERS, 'running')
    states.update(dict.fromkeys(services, 'exited'))
    states['postgres'] = 'running'
    local_backup.require_stopped_writers(states)


def inventory():
    from cli.local_persistence_acceptance import DOMAINS
    return {'schema_version': 1, 'domains': {
        name: {'count': 1, 'sha256': 'a' * 64} for name in DOMAINS}}


@pytest.mark.parametrize('domain', [
    'postgresql', 'uploaded_resumes', 'generated_resumes', 'cover_letters',
    'application_history', 'company_source_catalog', 'configuration'])
def test_every_domain_change_fails_without_echoing_values(domain):
    from cli.local_persistence_acceptance import compare
    before = inventory()
    after = copy.deepcopy(before)
    after['domains'][domain]['sha256'] = hashlib.sha256(b'private changed data').hexdigest()
    with pytest.raises(ValueError, match=domain) as error:
        compare(before, after)
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(schema_version=2),
    lambda v: v['domains'].pop('cover_letters'),
    lambda v: v['domains']['uploaded_resumes'].update(count=0),
    lambda v: v['domains']['configuration'].update(sha256='secret'),
    lambda v: v['domains']['postgresql'].update(count=True),
    lambda v: v.update(secret='must never serialize'),
])
def test_malformed_proof_is_rejected(mutation):
    from cli.local_persistence_acceptance import validate
    value = inventory()
    mutation(value)
    with pytest.raises(ValueError):
        validate(value)


def test_legacy_baseline_is_explicitly_not_run():
    from cli.local_persistence_acceptance import compare_baseline
    assert compare_baseline({}, None) == {'result': 'NOT_RUN', 'reason': 'Legacy baseline has no seven-domain inventory'}


def test_document_parser_rejects_other_shell_commands(tmp_path):
    from cli.local_persistence_acceptance import documented_stop_services
    path = tmp_path / 'runbook.md'
    path.write_text('docker compose -p $project stop api; echo unsafe\n')
    with pytest.raises(ValueError):
        documented_stop_services(path)


def test_inventory_is_only_opaque_counts_and_hashes():
    from cli.local_persistence_acceptance import compare
    result = compare(inventory(), inventory())
    assert result['result'] == 'PASS'
    assert 'configuration' in result['domains']
    assert 'a' * 64 not in json.dumps(result)


def test_backup_proof_refuses_running_source_and_uses_documented_stop(tmp_path):
    from cli.local_persistence_acceptance import require_running_backup_refusal
    from unittest.mock import patch
    with patch.object(local_backup, 'create_backup', side_effect=local_backup.BackupError('Stop ai-worker before backing up the database and documents')):
        assert require_running_backup_refusal('synthetic-source', tmp_path / 'archive') == 'PASS'
    with patch.object(local_backup, 'create_backup', return_value={}):
        with pytest.raises(RuntimeError, match='running writers'):
            require_running_backup_refusal('synthetic-source', tmp_path / 'archive')
    with patch.object(local_backup, 'create_backup', side_effect=local_backup.BackupError('unrelated failure')):
        with pytest.raises(local_backup.BackupError, match='unrelated'):
            require_running_backup_refusal('synthetic-source', tmp_path / 'archive')


def test_corrupt_archive_proof_preserves_source_bytes(tmp_path):
    from cli.local_persistence_acceptance import require_corrupt_restore_refusal
    from tests.unit.test_local_backup import LocalBackupTests
    archive = tmp_path / 'archive'
    archive.mkdir()
    LocalBackupTests.archive_fixture(archive)
    before = {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()}
    from unittest.mock import patch
    with patch.object(local_backup, '_compose', side_effect=AssertionError('Corrupt archive must never reach Docker')):
        assert require_corrupt_restore_refusal(archive, 'mjobs-fixture-012345abcdef', 18700) == 'PASS'
    assert {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()} == before


def test_occupied_restore_proof_has_no_mutation(tmp_path):
    from cli.local_persistence_acceptance import require_occupied_restore_refusal
    from tests.unit.test_local_backup import LocalBackupTests
    from unittest.mock import patch
    archive = tmp_path / 'archive'
    archive.mkdir()
    LocalBackupTests.archive_fixture(archive)
    with patch.object(local_backup, '_config', return_value={}), \
         patch.object(local_backup, '_compose', return_value=b'occupied-container') as compose, \
         patch.object(local_backup, '_volumes', return_value=[]), \
         patch.object(local_backup, '_run', side_effect=AssertionError('No Docker mutation')):
        assert require_occupied_restore_refusal(archive, 'mjobs-fixture-012345abcdef', 18700) == 'PASS'
    assert compose.call_count == 1
    assert compose.call_args.args[1:] == ('ps', '-a', '-q')


@pytest.mark.parametrize('module_name', ['compose_recovery', 'backup_acceptance'])
def test_invalid_extended_baseline_rejected_before_runtime(module_name, tmp_path):
    from cli import compose_recovery, backup_acceptance
    from unittest.mock import patch
    baseline = {'local_persistence': inventory()}
    baseline['local_persistence']['domains']['postgresql']['count'] = 0
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(baseline))
    with patch.object(compose_recovery, 'preflight_recovery_targets') as preflight:
        with pytest.raises(ValueError, match='postgresql'):
            if module_name == 'compose_recovery':
                compose_recovery.run(path, tmp_path / 'evidence', 'mjobs-fixture-012345abcdef', 18700)
            else:
                backup_acceptance.run(path, tmp_path / 'evidence', 'mjobs-fixture-012345abcdef',
                                      'mjobs-fixture-abcdef012345', 18700, tmp_path / 'archive')
        preflight.assert_not_called()


@pytest.mark.parametrize('extended', [False, True])
def test_existing_backup_entrypoint_preserves_legacy_and_orders_new_proof(extended, tmp_path, monkeypatch):
    from cli import backup_acceptance as backup
    from cli import local_persistence_acceptance as persistence
    from contextlib import nullcontext
    events = []
    baseline = {'profile_id': 'synthetic-profile', 'application_id': 'synthetic-app',
                'operation_id': 'synthetic-operation', 'request_id': 'synthetic-request',
                'external_submission_enabled': False,
                'confirmation': {'application_id': 'synthetic-app', 'receipt': 'MOCK-synthetic',
                                 'submission_count': 1}}
    if extended:
        baseline['local_persistence'] = inventory()
    path = tmp_path / 'baseline.json'
    path.write_text(json.dumps(baseline))
    monkeypatch.setattr(backup.recovery, 'preflight_recovery_targets', lambda *a, **k: {})
    monkeypatch.setattr(backup.recovery, 'verify_recovered_application', lambda *a: None)
    monkeypatch.setattr(backup.httpx, 'Client', lambda **k: nullcontext(object()))
    monkeypatch.setattr(backup.recovery, 'compose', lambda p, port, *a, **k: events.append(a))
    monkeypatch.setattr(local_backup, '_config', lambda *a: {})
    monkeypatch.setattr(local_backup, '_compose', lambda *a, **k: b'')
    monkeypatch.setattr(local_backup, '_volumes', lambda: [])
    monkeypatch.setattr(local_backup, '_wait_ready', lambda port: None)
    def observe(*args):
        assert extended, 'Legacy baseline must not fabricate domain observations'
        events.append(('observe',))
        return inventory()
    monkeypatch.setattr(persistence, 'observe_http', observe)
    for helper in ('require_running_backup_refusal', 'require_corrupt_restore_refusal',
                   'require_occupied_restore_refusal'):
        monkeypatch.setattr(persistence, helper, lambda *a, name=helper: events.append((name,)) or 'PASS')
    monkeypatch.setattr(local_backup, 'create_backup', lambda *a: events.append(('capture',)) or {
        'schema_version': 3, 'files': {local_backup.RECEIPT_PATH: 'b' * 64}})
    monkeypatch.setattr(local_backup, 'restore_backup', lambda *a: events.append(('restore',)))
    monkeypatch.setattr(backup.recovery, 'run', lambda *a: events.append(('receipt_recovery',)))
    evidence = backup.run(path, tmp_path / 'evidence.json', 'mjobs-fixture-012345abcdef',
                         'mjobs-fixture-abcdef012345', 18700, tmp_path / 'archive')
    stop = ('stop', *persistence.documented_stop_services())
    assert events.index(('require_running_backup_refusal',)) < events.index(stop) < events.index(('capture',))
    assert events.index(('require_corrupt_restore_refusal',)) < events.index(('restore',))
    assert events.index(('restore',)) < events.index(('require_occupied_restore_refusal',)) < events.index(('receipt_recovery',))
    assert ('stop', 'mock') in events
    assert ('down', '--remove-orphans') in events
    assert not any('-v' in event for event in events)
    assert evidence['local_persistence']['result'] == ('PASS' if extended else 'NOT_RUN')
    assert evidence['source_after_backup']['result'] == ('PASS' if extended else 'NOT_RUN')
    assert events.count(('observe',)) == (3 if extended else 0)
    assert events.count(stop) == (2 if extended else 1)
