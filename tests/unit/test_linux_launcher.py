"""Real Bash/Python bootstrap with Docker explicitly replaced at its boundary."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux launcher acceptance requires Linux')
class LinuxLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mjobs-launcher-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.root / '.devcontainer').mkdir()
        (self.root / 'cli').mkdir()
        shutil.copyfile(ROOT / '.devcontainer/mjobs.sh', self.root / '.devcontainer/mjobs.sh')
        shutil.copyfile(ROOT / 'cli/init_env.py', self.root / 'cli/init_env.py')
        self.bash = shutil.which('bash')
        self.assertIsNotNone(self.bash)
        for command in ('bash', 'dirname'):
            (self.bin / command).symlink_to(shutil.which(command))
        self.log = self.root / 'docker.jsonl'
        self.env = {**os.environ, 'PATH': str(self.bin), 'PYTHONPATH': '',
                    'LAUNCHER_TEST_LOG': str(self.log)}
        self.env.pop('PYTHONHOME', None)
        docker = self.bin / 'docker'
        docker.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ['LAUNCHER_TEST_LOG'], 'a') as log:
    log.write(json.dumps({{'args':sys.argv[1:], 'mode':os.environ.get('MJOBS_OPERATION_MODE'),
                          'mock':os.environ.get('MJOBS_COMPOSE_MOCK'),
                          'port':os.environ.get('MJOBS_PORT'), 'profiles':os.environ.get('COMPOSE_PROFILES')}}) + '\\n')
raise SystemExit(int(os.environ.get('LAUNCHER_TEST_COMPOSE_EXIT', '0')) if sys.argv[1] == 'compose' else 0)
''', encoding='utf-8')
        docker.chmod(0o700)

    def python(self, name='python3'):
        (self.bin / name).symlink_to(sys.executable)

    def launch(self, mode='fixture'):
        return subprocess.run([self.bash, str(self.root / '.devcontainer/mjobs.sh'), mode],
                              cwd=self.bin, env=self.env, capture_output=True, text=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_python3_only_bootstraps_complete_private_config_and_fixture(self):
        self.python()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root / '.env'
        values = dict(line.split('=', 1) for line in config.read_text().splitlines())
        self.assertGreaterEqual(len(values['POSTGRES_PASSWORD']), 32)
        self.assertEqual(len(base64.urlsafe_b64decode(values['MJOBS_CREDENTIAL_ENCRYPTION_KEY'])), 32)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        for value in values.values():
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(self.calls()[-1], dict(
            args=['compose', '-p', 'mjobs', '--profile', 'fixture', 'up', '-d', '--build', '--wait', '--wait-timeout', '180'],
            mode='FIXTURE', mock='1', port='18700', profiles='fixture'))
        self.assertIn('FIXTURE stack is ready', result.stdout)

    def test_existing_private_config_is_byte_identical_after_repeated_startup(self):
        self.python()
        config = self.root / '.env'
        original = b'POSTGRES_PASSWORD=existing-private-secret\nMJOBS_CREDENTIAL_ENCRYPTION_KEY=existing-private-key\n'
        config.write_bytes(original)
        for _ in range(2):
            result = self.launch()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_bytes(), original)
            self.assertNotIn('existing-private', result.stdout + result.stderr)

    def test_personal_mode_has_separate_project_port_and_no_fixture_profile(self):
        self.python()
        self.env['COMPOSE_PROFILES'] = 'fixture'
        result = self.launch('personal')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], dict(
            args=['compose', '-p', 'mjobs-personal', 'up', '-d', '--build', '--wait', '--wait-timeout', '180'],
            mode='PERSONAL', mock='0', port='18701', profiles=''))
        self.assertIn('Real external submission remains OFF', result.stdout)

    def test_python_alias_is_supported_when_python3_is_absent(self):
        self.python('python')
        self.assertEqual(self.launch().returncode, 0)
        self.assertTrue((self.root / '.env').is_file())

    def test_virtualenv_interpreter_takes_precedence(self):
        self.python()
        venv = self.root / '.venv/bin'
        venv.mkdir(parents=True)
        marker = self.root / 'venv-used'
        interpreter = venv / 'python'
        interpreter.write_text(f'#!{self.bash}\nprintf used >> "{marker}"\nexec "{sys.executable}" "$@"\n')
        interpreter.chmod(0o700)
        self.assertEqual(self.launch().returncode, 0)
        self.assertTrue(marker.exists())

    def test_missing_or_unsupported_interpreter_fails_before_config_or_docker(self):
        for unsupported in (False, True):
            with self.subTest(unsupported=unsupported):
                if unsupported:
                    interpreter = self.bin / 'python3'
                    # Run the launcher's real probe against a simulated 3.9
                    # version; normal -m execution would still work if the
                    # launcher accidentally omitted its compatibility check.
                    interpreter.write_text(f'''#!{self.bash}
if [[ "$1" == "-c" ]]; then
  exec "{sys.executable}" -c 'import sys; sys.version_info = (3, 9, 0); exec(sys.argv[1])' "$2"
fi
exec "{sys.executable}" "$@"
''')
                    interpreter.chmod(0o700)
                result = self.launch()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Python 3.10 or newer', result.stderr)
                self.assertFalse((self.root / '.env').exists())
                self.assertEqual(self.calls(), [])

    def test_initializer_failure_does_not_invoke_docker_or_report_readiness(self):
        self.python()
        (self.root / 'cli/init_env.py').write_text('raise RuntimeError("synthetic initializer failure")\n')
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('synthetic initializer failure', result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertNotIn('stack is ready', result.stdout)

    def test_compose_failure_does_not_report_readiness(self):
        self.python()
        self.env['LAUNCHER_TEST_COMPOSE_EXIT'] = '9'
        result = self.launch()
        self.assertEqual(result.returncode, 9)
        self.assertNotIn('stack is ready', result.stdout)


if __name__ == '__main__':
    unittest.main()
