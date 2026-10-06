"""Private project configuration is published atomically and never overwritten."""
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli import init_env
from cli.init_env import initialize


class InitEnvTests(unittest.TestCase):
    def test_cli_reports_creation_and_preservation_without_printing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('cli.init_env.ROOT',Path(directory)), patch('cli.init_env.secrets.token_urlsafe',return_value='synthetic-secret'):
                first=StringIO()
                with redirect_stdout(first):self.assertEqual(init_env.main(),0)
                second=StringIO()
                with redirect_stdout(second):self.assertEqual(init_env.main(),0)
            self.assertEqual(first.getvalue(),'Created project-local ignored .env\n')
            self.assertEqual(second.getvalue(),'Existing .env preserved\n')
            self.assertNotIn('synthetic-secret',first.getvalue()+second.getvalue())
            self.assertEqual((Path(directory)/'.env').read_text(encoding='utf-8'),'POSTGRES_PASSWORD=synthetic-secret\n')

    def test_creates_complete_private_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            created=initialize(path,secret_factory=lambda _size:'synthetic-secret')
            self.assertTrue(created)
            self.assertEqual(path.read_text(encoding='utf-8'),'POSTGRES_PASSWORD=synthetic-secret\n')
            if os.name!='nt':
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(list(Path(directory).glob('.env.init-*.tmp')),[])

    def test_existing_configuration_is_preserved_without_generating_a_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env';path.write_bytes(b'EXISTING_PRIVATE_CONFIGURATION\n')
            initializer=initialize
            with patch('cli.init_env.secrets.token_urlsafe',side_effect=AssertionError('must not generate')):
                self.assertFalse(initializer(path))
            self.assertEqual(path.read_bytes(),b'EXISTING_PRIVATE_CONFIGURATION\n')
            self.assertEqual(list(Path(directory).glob('.env.init-*.tmp')),[])

    def test_publish_failure_leaves_no_partial_configuration_or_staging_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            with patch('cli.init_env.os.link',side_effect=OSError('synthetic publish failure')):
                with self.assertRaisesRegex(OSError,'synthetic publish failure'):
                    initialize(path,secret_factory=lambda _size:'synthetic-secret')
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).glob('.env.init-*.tmp')),[])

    def test_staging_sync_failure_leaves_no_partial_configuration_or_staging_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            with patch('cli.init_env.os.fsync',side_effect=OSError('synthetic sync failure')):
                with self.assertRaisesRegex(OSError,'synthetic sync failure'):
                    initialize(path,secret_factory=lambda _size:'synthetic-secret')
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).glob('.env.init-*.tmp')),[])

    def test_competing_initializer_does_not_replace_the_published_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            real_link=os.link
            def publish_other_winner(source,destination):
                Path(destination).write_bytes(b'WINNING_CONFIGURATION\n')
                real_link(source,destination)
            with patch('cli.init_env.os.link',side_effect=publish_other_winner):
                self.assertFalse(initialize(path,secret_factory=lambda _size:'losing-secret'))
            self.assertEqual(path.read_bytes(),b'WINNING_CONFIGURATION\n')
            self.assertEqual(list(Path(directory).glob('.env.init-*.tmp')),[])


if __name__=='__main__':
    unittest.main()
