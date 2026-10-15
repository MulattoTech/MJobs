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
@pytest.mark.parametrize('failure', [None, 'backup', 'verify', 'restore', 'file_count'])
def test_existing_backup_entrypoint_preserves_legacy_and_orders_new_proof(extended, failure, tmp_path, monkeypatch):
    from cli import backup_acceptance as backup
    from cli import local_persistence_acceptance as persistence
    from contextlib import nullcontext
    import sys
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
    def public_command(command, **kwargs):
        assert command[:3] == [sys.executable, '-m', 'cli.local_backup']
        assert kwargs['environment'] == {}
        assert kwargs['timeout'] > 0
        action = command[3]
        if action == failure:
            events.append(('public_failure', action))
            raise RuntimeError('Public ' + action + ' CLI failed with exit code 2')
        if action == 'backup':
            assert command[4:] == ['--project', 'mjobs-fixture-012345abcdef', '--archive', str(tmp_path / 'archive')]
            events.append(('capture',))
            return 'Backup created: 1 files; mode FIXTURE\nConfiguration snapshot: non-secret runtime settings saved for manual review\n'
        if action == 'verify':
            assert command[4:] == ['--archive', str(tmp_path / 'archive')]
            events.append(('verify',))
            if failure == 'file_count':
                return 'Backup verified: 2 files; mode FIXTURE\n'
            return 'Backup verified: 1 files; mode FIXTURE\n'
        assert action == 'restore'
        assert command[4:] == ['--archive', str(tmp_path / 'archive'), '--target-project',
                               'mjobs-fixture-abcdef012345', '--port', '18700']
        events.append(('restore',))
        return 'Restored FIXTURE project mjobs-fixture-abcdef012345 at 127.0.0.1:18700\n'
    monkeypatch.setattr(backup.recovery, '_run_bounded', public_command)
    monkeypatch.setattr(local_backup, 'verify_backup', lambda *a: {
        'schema_version': 3, 'files': {local_backup.RECEIPT_PATH: 'b' * 64}})
    def forbidden_positive_helper(*args):
        raise AssertionError('Positive backup/restore proof must execute public CLI, not call engine helper')
    monkeypatch.setattr(local_backup, 'create_backup', forbidden_positive_helper)
    monkeypatch.setattr(local_backup, 'restore_backup', forbidden_positive_helper)
    monkeypatch.setattr(backup.recovery, 'run', lambda *a: events.append(('receipt_recovery',)))
    if failure:
        with pytest.raises(RuntimeError, match='file counts differ' if failure == 'file_count' else 'exit code 2'):
            backup.run(path, tmp_path / 'evidence.json', 'mjobs-fixture-012345abcdef',
                       'mjobs-fixture-abcdef012345', 18700, tmp_path / 'archive')
        assert not (tmp_path / 'evidence.json').exists()
        assert ('receipt_recovery',) not in events
        assert ('require_occupied_restore_refusal',) not in events
        if failure in {'backup', 'verify', 'file_count'}:
            assert ('require_corrupt_restore_refusal',) not in events
            assert ('down', '--remove-orphans') not in events
        return
    evidence = backup.run(path, tmp_path / 'evidence.json', 'mjobs-fixture-012345abcdef',
                         'mjobs-fixture-abcdef012345', 18700, tmp_path / 'archive')
    stop = ('stop', *persistence.documented_stop_services())
    assert events.index(('require_running_backup_refusal',)) < events.index(stop) < events.index(('capture',))
    assert events.index(('capture',)) < events.index(('verify',)) < events.index(('require_corrupt_restore_refusal',))
    assert events.index(('require_corrupt_restore_refusal',)) < events.index(('restore',))
    assert events.index(('restore',)) < events.index(('require_occupied_restore_refusal',)) < events.index(('receipt_recovery',))
    assert ('stop', 'mock') in events
    assert ('down', '--remove-orphans') in events
    assert not any('-v' in event for event in events)
    assert evidence['local_persistence']['result'] == ('PASS' if extended else 'NOT_RUN')
    assert evidence['source_after_backup']['result'] == ('PASS' if extended else 'NOT_RUN')
    assert events.count(('observe',)) == (3 if extended else 0)
    assert events.count(stop) == (2 if extended else 1)
    assert set(evidence['public_cli']) == {'backup', 'verify', 'restore'}
    assert all(row['exit_code'] == 0 and row['result'] == 'PASS' for row in evidence['public_cli'].values())
    assert all(row['status'] == 'PASS' for row in evidence['public_cli'].values())
    assert all('stdout' not in row and 'environment' not in row for row in evidence['public_cli'].values())


@pytest.mark.parametrize('output', ['', 'Backup refused: private diagnostic\n',
                                  'Backup verified: 0 files; mode FIXTURE\n',
                                  'Backup verified: 2 files; mode PERSONAL\n',
                                  'Backup verified: 2 files; mode FIXTURE\nprivate unexpected output\n'])
def test_public_cli_requires_expected_positive_status_without_echo(output, tmp_path, monkeypatch):
    from cli import backup_acceptance as backup
    monkeypatch.setattr(backup.recovery, '_run_bounded', lambda *a, **k: output)
    with pytest.raises(RuntimeError, match='expected positive status') as error:
        backup.run_public_cli('verify', tmp_path / 'archive', environment={})
    assert 'private' not in str(error.value)


def test_public_cli_propagates_nonzero_without_emitting_positive_evidence(tmp_path, monkeypatch):
    from cli import backup_acceptance as backup
    def failed(*args, **kwargs):
        raise RuntimeError('Public verify CLI failed with exit code 2')
    monkeypatch.setattr(backup.recovery, '_run_bounded', failed)
    with pytest.raises(RuntimeError, match='exit code 2'):
        backup.run_public_cli('verify', tmp_path / 'archive', environment={})


@pytest.mark.parametrize('subcommand,project,port', [
    ('create', None, None), ('backup', 'mjobs-personal', None),
    ('restore', 'mjobs-personal', 18700), ('restore', 'mjobs-fixture-012345abcdef', 80),
])
def test_public_command_rejects_unadmitted_target_before_spawn(subcommand, project, port, tmp_path):
    from cli import backup_acceptance as backup
    from unittest.mock import patch
    with patch.object(backup.recovery, '_run_bounded') as runner:
        with pytest.raises(ValueError):
            backup.run_public_cli(subcommand, tmp_path / 'archive', environment={}, project=project, port=port)
        runner.assert_not_called()


def test_public_verify_executes_actual_parser_and_checks_nonzero_offline(tmp_path):
    from cli import backup_acceptance as backup
    from tests.unit.test_local_backup import LocalBackupTests
    import os
    archive = tmp_path / 'synthetic-archive'
    archive.mkdir()
    manifest = LocalBackupTests.archive_fixture(archive)
    manifest['operation_mode'] = 'FIXTURE'
    (archive / 'manifest.json').write_text(json.dumps(manifest))
    proof = backup.run_public_cli('verify', archive, environment=os.environ.copy())
    assert proof['exit_code'] == 0 and proof['file_count'] == 2
    assert proof['command'][1:4] == ['-m', 'cli.local_backup', 'verify']
    (archive / 'database.dump').write_bytes(b'corrupt synthetic dump')
    with pytest.raises(RuntimeError, match='exit code 2') as error:
        backup.run_public_cli('verify', archive, environment=os.environ.copy())
    assert 'database.dump' not in str(error.value)  # Child diagnostics stay captured.
