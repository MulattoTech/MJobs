"""Safe backup failure retention and rollback, without Docker or live data."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from infra.deploy.windows import update_mjobs_micro as cd


PRIVATE = 'SYNTHETIC_PRIVATE_BACKUP_DIAGNOSTIC'


class BackupDiagnosticTests(unittest.TestCase):
    def failure(self, stderr, *, backup_diagnostics=True):
        result = subprocess.CompletedProcess([], 2, stdout=PRIVATE, stderr=stderr)
        with patch.object(cd, '_run_captured', return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                cd.command(['synthetic-tool', '--credential', PRIVATE],
                           env={'SYNTHETIC_PRIVATE_VALUE': PRIVATE},
                           label='quiescent backup', backup_diagnostics=backup_diagnostics)
        self.assertNotIn(PRIVATE, str(caught.exception))
        return caught.exception

    def test_known_exit_two_retains_only_a_fixed_reason(self):
        cases = (
            ('PostgreSQL must be running for a logical backup', 'BACKUP_POSTGRES_NOT_RUNNING'),
            ('Project-local .env is missing; run python -m cli.init_env first', 'BACKUP_ENV_MISSING'),
            ('Backup destination already exists; choose a new path', 'BACKUP_DESTINATION_EXISTS'),
            ('Backup runtime configuration contains unsupported settings', 'BACKUP_RUNTIME_CONFIGURATION_INVALID'),
            ('Database backup metadata is missing or ambiguous', 'BACKUP_DATABASE_METADATA_INVALID'),
            ('Backup contains unlisted or missing files', 'BACKUP_ARCHIVE_INVALID'),
        )
        for detail, code in cases:
            with self.subTest(code=code):
                error = self.failure('Backup refused: ' + detail + '\n')
                self.assertEqual(str(error), f'quiescent backup failed (exit 2) [{code}]')

    def test_fixed_services_and_actions_are_classified(self):
        cases = (
            ('Stop source-worker before backing up the database and documents',
             'BACKUP_WRITER_SOURCE_WORKER_NOT_STOPPED'),
            ('Stop migrate before backing up the database and documents',
             'BACKUP_WRITER_MIGRATE_NOT_STOPPED'),
            ('Stop mock before backing up fixture receipts', 'BACKUP_FIXTURE_NOT_STOPPED'),
            ('Exactly one api container is required', 'BACKUP_API_CONTAINER_AMBIGUOUS'),
            ('Dump PostgreSQL failed with exit code 1; inspect Docker logs', 'BACKUP_DATABASE_DUMP_FAILED'),
            ('Copy project documents failed with exit code -9; inspect Docker logs', 'BACKUP_COPY_DOCUMENTS_FAILED'),
            ('Inspect project services could not complete; inspect Docker state', 'BACKUP_INSPECT_SERVICES_UNAVAILABLE'),
            ('Inspect API runtime configuration exceeded the captured-output limit',
             'BACKUP_INSPECT_RUNTIME_CONFIGURATION_OUTPUT_LIMIT'),
        )
        for detail, code in cases:
            with self.subTest(code=code):
                self.assertEqual(cd.backup_failure_code('Backup refused: ' + detail + '\r\n'), code)

    def test_unrecognized_or_forged_output_never_enters_the_error(self):
        known = 'Backup refused: PostgreSQL must be running for a logical backup'
        cases = (
            '', PRIVATE, None,
            known + '\n' + PRIVATE,
            PRIVATE + '\n' + known,
            known + ' ' + PRIVATE,
            known + '\n\n',
            'Backup refused: Stop ' + PRIVATE + ' before backing up the database and documents',
            'Backup refused: ' + PRIVATE + ' failed with exit code 2; inspect Docker logs',
            'Backup refused: Dump PostgreSQL failed with exit code ' + PRIVATE + '; inspect Docker logs',
            'Backup refused: Dump PostgreSQL failed with exit code 123456789012; inspect Docker logs',
            'Backup refused: Exactly one ' + PRIVATE + ' container is required',
            'Backup refused: Backup checksum mismatch: documents/' + PRIVATE + '.pdf',
            PRIVATE * 1000,
            '\N{SNOWMAN}' * 3000,
        )
        for index, stderr in enumerate(cases):
            with self.subTest(case=index):
                error = self.failure(stderr)
                self.assertEqual(str(error),
                                 'quiescent backup failed (exit 2) [BACKUP_FAILURE_UNCLASSIFIED]')

    def test_success_and_nonbackup_failure_keep_the_existing_contract(self):
        result = subprocess.CompletedProcess([], 0, stdout='  verified\n', stderr=PRIVATE)
        with patch.object(cd, '_run_captured', return_value=result):
            self.assertEqual(cd.command(['synthetic-tool'], backup_diagnostics=True), 'verified')
        error = self.failure('Backup refused: PostgreSQL must be running for a logical backup\n',
                             backup_diagnostics=False)
        self.assertEqual(str(error), 'quiescent backup failed (exit 2)')

    def test_all_command_timeouts_are_safe_and_backup_keeps_its_fixed_code(self):
        timeout = cd.CommandCaptureError('COMMAND_TIMEOUT')
        with patch.object(cd, '_run_captured', side_effect=timeout):
            with self.assertRaises(RuntimeError) as caught:
                cd.command(['synthetic-tool', PRIVATE], label='verify backup', backup_diagnostics=True)
            with self.assertRaises(RuntimeError) as original:
                cd.command(['synthetic-tool', PRIVATE])
        self.assertEqual(str(caught.exception), 'verify backup timed out [BACKUP_COMMAND_TIMEOUT]')
        self.assertNotIn(PRIVATE, ''.join(traceback.format_exception(caught.exception)))
        self.assertEqual(str(original.exception), 'command could not complete [COMMAND_TIMEOUT]')
        self.assertNotIn(PRIVATE, ''.join(traceback.format_exception(original.exception)))

    def test_main_retains_the_safe_reason_in_the_existing_log(self):
        error = self.failure('Backup refused: Backup runtime configuration contains unsupported settings\n')
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            locking = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=lambda *_: None)
            with patch.object(cd, 'HOME', home), patch.object(cd, 'LOG', home / 'update.log'), \
                    patch.object(cd, 'os', SimpleNamespace(name='nt', SEEK_END=os.SEEK_END)), \
                    patch.object(cd, 'msvcrt', locking), patch.object(cd, 'run_update', side_effect=error):
                self.assertEqual(cd.main(), 1)
            retained = (home / 'update.log').read_text(encoding='utf-8')
        self.assertIn('BLOCKED: quiescent backup failed (exit 2) [BACKUP_RUNTIME_CONFIGURATION_INVALID]', retained)
        self.assertNotIn(PRIVATE, retained)
        self.assertNotIn('Backup refused:', retained)


class BackupRollbackTests(unittest.TestCase):
    def test_backup_or_verification_failure_restores_previous_image_without_state_update(self):
        for phase in ('backup', 'verify'):
            for timed_out in (False, True):
                with self.subTest(phase=phase, timeout=timed_out), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    home, base, old_source, source = (root / name for name in ('home', 'base', 'old', 'new'))
                    for path in (home, base, old_source, source):
                        path.mkdir()
                    (source / 'compose.yml').write_text('MJOBS_PUBLIC_HOST MJOBS_IMAGE\n', encoding='utf-8')
                    for name in ('.env', 'compose.micro.yml'):
                        (base / name).write_text('synthetic fixture\n', encoding='utf-8')
                    python = root / 'synthetic-python'
                    python.touch()
                    old_sha, sha = 'a' * 40, 'b' * 40
                    old_image = 'mjobs-micro:sha-' + old_sha
                    state = home / 'state.json'
                    state.write_text(json.dumps({'sha': old_sha, 'image': old_image,
                                                 'source': str(old_source)}), encoding='utf-8')
                    original_state = state.read_bytes()

                    def process(args, **_kwargs):
                        if len(args) > 3 and args[1:3] == ['-m', 'cli.local_backup'] and args[3] == phase:
                            if timed_out:
                                raise cd.CommandCaptureError('COMMAND_TIMEOUT')
                            return subprocess.CompletedProcess(args, 2, stdout=PRIVATE,
                                stderr='Backup refused: Backup runtime configuration is invalid\n')
                        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

                    with patch.object(cd, 'HOME', home), patch.object(cd, 'BASE', base), \
                            patch.object(cd, 'STATE', state), patch.object(cd, 'OPS_PYTHON', python), \
                            patch.object(cd, 'main_sha', return_value=sha), \
                            patch.object(cd, 'checks_pass', return_value=True), \
                            patch.object(cd, 'prepare_candidate', return_value=source), \
                            patch.object(cd, 'migration_digest', return_value='same-migrations'), \
                            patch.object(cd, 'verify_candidate'), patch.object(cd, 'require_plain_path'), \
                            patch.object(cd, '_run_captured', side_effect=process), \
                            patch.object(cd, 'compose') as compose, patch.object(cd, 'ready') as ready, \
                            patch.object(cd, 'atomic_state') as save_state, patch.object(cd, 'log') as log:
                        with self.assertRaises(RuntimeError) as caught:
                            cd.run_update()
                    expected = 'BACKUP_COMMAND_TIMEOUT' if timed_out else 'BACKUP_RUNTIME_CONFIGURATION_INVALID'
                    self.assertIn(expected, str(caught.exception))
                    self.assertNotIn(PRIVATE, str(caught.exception))
                    self.assertEqual(compose.call_args_list, [
                        call(old_source, old_sha, old_image, 'stop', *cd.WRITERS, timeout=180),
                        call(old_source, old_sha, old_image, 'up', '-d', '--no-build', timeout=300),
                    ])
                    ready.assert_called_once_with(old_sha)
                    save_state.assert_not_called()
                    self.assertEqual(state.read_bytes(), original_state)
                    self.assertIn(call('Rollback healthy'), log.call_args_list)


if __name__ == '__main__':
    unittest.main()
