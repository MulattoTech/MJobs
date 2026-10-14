"""Real Bash rejection checks: bad startup input must not initialize config or Docker."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which('bash') if os.name != 'nt' else r'C:\Program Files\Git\bin\bash.exe'


@unittest.skipUnless(BASH and Path(BASH).is_file(), 'Bash is unavailable')
class LauncherAdmissionTests(unittest.TestCase):
    def test_invalid_mode_project_and_port_fail_before_side_effects(self):
        cases = [('unknown', {}, 'Usage:'),
                 ('fixture', {'COMPOSE_PROJECT_NAME': 'unrelated-production'}, 'project'),
                 ('fixture', {'MJOBS_PORT': 'not-a-port'}, 'port'),
                 ('personal', {'MJOBS_PORT': '0'}, 'port'),
                 ('fixture', {'MJOBS_PORT': '1023'}, 'port'),
                 ('personal', {'COMPOSE_PROJECT_NAME': 'mjobs-fixture-012345abcdef'}, 'project'),
                 ('fixture', {'MJOBS_PORT': '65536'}, 'port'),
                 ('fixture', {'MJOBS_PORT': '018700'}, 'port')]
        for mode, overrides, diagnostic in cases:
            with self.subTest(mode=mode, overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / '.devcontainer').mkdir()
                shutil.copyfile(ROOT / '.devcontainer/mjobs.sh', root / '.devcontainer/mjobs.sh')
                # An accidental initializer or Docker call cannot succeed, and
                # must not hide the expected admission diagnostic.
                (root / 'cli').mkdir()
                (root / 'cli/init_env.py').write_text('raise RuntimeError("initializer called before validation")\n')
                env = {k: v for k, v in os.environ.items() if k not in ('MJOBS_PORT', 'COMPOSE_PROJECT_NAME')}
                env.update(overrides)
                result = subprocess.run([BASH, str(root / '.devcontainer/mjobs.sh'), mode],
                                        cwd=root, env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stderr)
                self.assertNotIn('initializer called', result.stderr)
                self.assertFalse((root / '.env').exists())
