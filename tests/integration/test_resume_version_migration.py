"""Upgrade preserves uploaded originals and gives them durable version identity."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.documents.uploads import list_resumes, save_resume, read_resume


class ResumeVersionMigrationTests(unittest.TestCase):
    def test_legacy_resume_backfill_retains_original_and_refuses_history_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = f"sqlite:///{(root / 'old.db').as_posix()}"
            config = Config('alembic.ini')
            config.set_main_option('sqlalchemy.url', url.replace('%','%%'))
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL':url}):
                command.upgrade(config, 'b71d9a42c0e3')
                store = Store(url)
                try:
                    with store.engine.begin() as c:
                        c.execute(text("INSERT INTO career_profiles (id,name,email,version) VALUES ('legacy','Legacy Example','legacy@example.invalid','v1')"))
                        c.execute(m.profile_versions.insert().values(id=identity('legacy','v1'),
                            profile_id='legacy',version='v1',contact={'name':'Legacy Example','email':'legacy@example.invalid'},answers=[]))
                        c.execute(m.uploaded_resumes.insert().values(id='old-upload',profile_id='legacy',filename='original.txt',format='txt',
                            media_type='text/plain',size_bytes=8,sha256='a'*64,status='STORED_UNREVIEWED',created_at='2026-10-01T00:00:00Z'))
                    command.upgrade(config, 'head')
                    originals = list_resumes(store, 'legacy')
                    self.assertEqual(len(originals), 1)
                    self.assertEqual((originals[0]['id'], originals[0]['version'], originals[0]['label']), ('old-upload',1,'original.txt'))
                    uploaded = save_resume(store, root, 'legacy', 'new.txt', 'text/plain', b'New content', series_id='old-upload')
                    self.assertEqual(uploaded['version'], 2)
                    self.assertEqual(read_resume(store, root, 'legacy', uploaded['id'])[1], b'New content')
                    self.assertEqual(len(list_resumes(store, 'legacy')), 2)
                    with self.assertRaisesRegex(RuntimeError, 'Refusing to discard resume version history'):
                        command.downgrade(config, 'b71d9a42c0e3')
                finally:
                    store.engine.dispose()
