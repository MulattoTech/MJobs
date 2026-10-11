"""Offline dependency-entry-point regressions; never install or contact a registry."""
from pathlib import Path
import tempfile
import unittest
from tools.ci.python_requirements import requirement_files, requirement_lines
from tools.agent_dashboard.agent_environment import _dependencies_match
from tools.verification.gpu_independence import validate_gpu_independence
from tests.unit import test_gpu_independence


class PythonRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def test_follow_legacy_entry_to_canonical_pins(self):
        self.write('requirements.lock', '# compatibility\n-r requirements.txt\n')
        self.write('requirements.txt', 'fastapi==1.0.0\nhttpx==2.0.0\n')
        self.assertEqual(set(requirement_files(self.root)), {'requirements.lock', 'requirements.txt'})
        self.assertEqual(requirement_lines(self.root), ('fastapi==1.0.0', 'httpx==2.0.0'))

    def test_legacy_inline_manifest_still_works(self):
        self.write('requirements.lock', 'fastapi==1.0.0\n')
        self.assertEqual(requirement_lines(self.root), ('fastapi==1.0.0',))

    def test_missing_include_is_rejected(self):
        self.write('requirements.lock', '-r missing.txt\n')
        with self.assertRaises(ValueError):
            requirement_files(self.root)

    def test_traversal_absolute_url_and_cycle_rejected(self):
        for value in ('../outside.txt', '/tmp/outside.txt', 'https://example.com/secret.txt', 'requirements.lock'):
            with self.subTest(value=value):
                self.write('requirements.lock', f'-r {value}\n')
                with self.assertRaises(ValueError):
                    requirement_files(self.root)

    def test_nested_includes_resolve_relative_to_the_including_file(self):
        self.write('requirements.lock', '-r dependencies/runtime.txt\n')
        self.write('dependencies/runtime.txt', '--requirement base.txt\n')
        self.write('dependencies/base.txt', 'httpx==2.0.0\n')
        self.assertEqual(requirement_lines(self.root), ('httpx==2.0.0',))

    def test_dependency_change_is_not_hidden_by_identical_wrappers(self):
        a, b = self.root / 'a', self.root / 'b'
        for folder in (a, b):
            folder.mkdir()
            (folder / 'requirements.lock').write_text('-r requirements.txt\n')
        (a / 'requirements.txt').write_text('httpx==1.0.0\n')
        (b / 'requirements.txt').write_text('httpx==2.0.0\n')
        self.assertFalse(_dependencies_match(a, b))

    def test_same_pins_can_share_environment_across_entry_point_migration(self):
        a, b = self.root / 'a', self.root / 'b'
        a.mkdir(); b.mkdir()
        (a / 'requirements.lock').write_text('httpx==1.0.0\n')
        (b / 'requirements.lock').write_text('-r requirements.txt\n')
        (b / 'requirements.txt').write_text('# comment\nhttpx==1.0.0\n')
        self.assertTrue(_dependencies_match(a, b))

    def test_gpu_guard_checks_included_manifest(self):
        root = test_gpu_independence.GpuIndependenceTests().make_repository(self.root)
        self.write('requirements.lock', '-r requirements.txt\n')
        self.write('requirements.txt', 'torch==2.0.0\n')
        self.assertIn('GPU_DEPENDENCY:requirements.txt:torch', validate_gpu_independence(root))

    def test_bounded_manifest_size(self):
        self.write('requirements.lock', 'a' * 150000)
        with self.assertRaises(ValueError):
            requirement_files(self.root)


if __name__ == '__main__':
    unittest.main()
