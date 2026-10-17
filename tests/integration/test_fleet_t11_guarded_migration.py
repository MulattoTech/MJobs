"""Contained CLI policy and real synthetic SQLite migration/restore rehearsal.

No Docker or PostgreSQL connection is made. Native PG backup/Compose remains
separate acceptance, not implied by the SQLite rehearsal or mocked CLI driver.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from contextlib import ExitStack, closing

from alembic import command
from alembic.config import Config
from cli import migration_precondition as guard
from cli import provision_database


class GuardedEntryPointTests(unittest.TestCase):
    def exercise(self, directory, *, failure=None):
        root=Path(directory); run_id='a'*32; nonce='b'*64; run=root/run_id; run.mkdir()
        dump=b'synthetic full-dump bytes, NOT a PostgreSQL archive'
        (run/'database.dump').write_bytes(dump)
        destination=root/'runtime-url'; destination.write_bytes(b'original synthetic credential')
        anchor={'id':'workspace','operation_mode':'PERSONAL','bound_at':'2026-10-17T00:00:00Z'}
        identity={'system_identifier':'123','database':'mjobs_test','database_oid':42}
        now=int(time.time())
        required=guard.required_signature()
        receipt={'version':1,'run_id':run_id,'nonce_hash':hashlib.sha256(nonce.encode()).hexdigest(),
            'issued_at':now,'expires_at':now+900,'source_identity':identity,'source_heads':['old'],
            'target_head':'new','code_hash':guard.code_hash(),'schema_hash':guard.digest(required),
            'anchor':anchor,'dump_sha256':hashlib.sha256(dump).hexdigest(),'dump_size':len(dump),
            'archive_sha256':'e'*64,'project':'synthetic','postgres_container':'f'*64,
            'database_volume':'synthetic_database'}
        if failure=='wrong_target': receipt['source_identity']=dict(identity,database_oid=99)
        if failure=='corrupt': (run/'database.dump').write_bytes(b'corrupted')
        (run/'receipt.ready').write_text(json.dumps(receipt))
        observed=copy.deepcopy(required)
        if failure=='schema': observed['tables']['resume_workspace_revisions']['pk']=[]
        after_anchor=None if failure=='workspace' else anchor
        events=[]; engine=MagicMock(); script=MagicMock(); script.get_heads.return_value=['new']
        from sqlalchemy.dialects.postgresql import dialect
        engine.connect.return_value.__enter__.return_value.dialect=dialect()
        script.walk_revisions.return_value=[type('Revision',(),{'revision':'old'})()]
        stdin=MagicMock(); stdin.buffer=io.BytesIO(json.dumps({'nonce':nonce}).encode())
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'MJOBS_DATABASE_URL':
                'postgresql+psycopg://admin:synthetic@invalid/mjobs_test',
                'MJOBS_DATABASE_URL_FILE':str(destination)},clear=True))
            stack.enter_context(patch.object(sys,'argv',['provision','--migration-run',run_id]))
            stack.enter_context(patch.object(sys,'stdin',stdin))
            stack.enter_context(patch.object(guard,'RUN_ROOT',root))
            stack.enter_context(patch.object(guard,'require_private_run'))  # container UID contract tested separately
            stack.enter_context(patch.object(guard,'create_engine',return_value=engine))
            stack.enter_context(patch.object(guard.ScriptDirectory,'from_config',return_value=script))
            stack.enter_context(patch.object(guard,'current_heads',side_effect=[['old'],['new']]))
            stack.enter_context(patch.object(guard,'workspace_anchor',side_effect=[anchor,after_anchor]))
            stack.enter_context(patch.object(guard,'database_identity',return_value=identity))
            stack.enter_context(patch.object(guard,'observed_signature',return_value=observed))
            def upgrade(*args):
                self.assertTrue((run/'receipt.consumed').exists())
                events.append('upgrade')
            stack.enter_context(patch.object(guard.command,'upgrade',side_effect=upgrade))
            def publish(*args): events.append('provision')
            stack.enter_context(patch.object(provision_database,'provision',side_effect=publish))
            status=provision_database.main()
        self.assertEqual((run/'database.dump').read_bytes(),b'corrupted' if failure=='corrupt' else dump)
        self.assertEqual(destination.read_bytes(),b'original synthetic credential')
        return status,events,run

    def test_wrong_target_or_corrupt_backup_blocks_upgrade_and_preserves_credentials(self):
        for failure in ('wrong_target','corrupt'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                status,events,run=self.exercise(directory,failure=failure)
                self.assertEqual(status,1); self.assertEqual(events,[])
                self.assertTrue((run/'receipt.ready').exists())

    def test_schema_or_workspace_validation_failure_never_provisions(self):
        for failure in ('schema','workspace'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                status,events,run=self.exercise(directory,failure=failure)
                self.assertEqual(status,1); self.assertEqual(events,['upgrade'])
                self.assertTrue((run/'receipt.consumed').exists())

    def test_validated_authorized_entrypoint_provisions_after_one_use_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            status,events,run=self.exercise(directory)
            self.assertEqual(status,0); self.assertEqual(events,['upgrade','provision'])
            self.assertTrue((run/'receipt.consumed').exists())


class SyntheticSQLiteMigrationRehearsalTests(unittest.TestCase):
    def test_drop_obsolete_column_preserves_sentinel_anchor_ciphertext_and_separate_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); versions=root/'versions'; versions.mkdir()
            (root/'env.py').write_text('''from alembic import context
from sqlalchemy import create_engine, event
engine=create_engine(context.config.get_main_option('sqlalchemy.url'))
@event.listens_for(engine,'begin')
def begin(c): c.exec_driver_sql('BEGIN')
with engine.connect() as connection:
    context.configure(connection=connection,transactional_ddl=True)
    with context.begin_transaction(): context.run_migrations()
engine.dispose()
''')
            (versions/'old.py').write_text('''from alembic import op
revision='old'
down_revision=None
def upgrade():
    op.execute('CREATE TABLE business_sentinel (id INTEGER PRIMARY KEY,value TEXT NOT NULL,obsolete TEXT)')
    op.execute('CREATE TABLE encrypted_rows (id INTEGER PRIMARY KEY,cipher BLOB NOT NULL)')
    op.execute('CREATE TABLE workspace_identity (id TEXT PRIMARY KEY,operation_mode TEXT NOT NULL,bound_at TEXT NOT NULL)')
def downgrade(): raise AssertionError('source downgrade is forbidden')
''')
            (versions/'new.py').write_text('''from alembic import op
revision='new'
down_revision='old'
def upgrade(): op.drop_column('business_sentinel','obsolete')
def downgrade(): raise AssertionError('source downgrade is forbidden')
''')
            source=root/'source.db'; backup=root/'preupgrade.db'; restored=root/'fresh-restored.db'
            config=Config(); config.set_main_option('script_location',str(root))
            config.set_main_option('sqlalchemy.url','sqlite:///'+source.as_posix())
            command.upgrade(config,'old')
            anchor=('workspace','PERSONAL','2026-10-17T00:00:00Z')
            cipher=b'MJCR1synthetic-ciphertext-only'
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute("INSERT INTO business_sentinel VALUES (1,'preserve-me','old-value')")
                connection.execute('INSERT INTO encrypted_rows VALUES (1,?)',(cipher,))
                connection.execute('INSERT INTO workspace_identity VALUES (?,?,?)',anchor)
            with closing(sqlite3.connect(source)) as origin,closing(sqlite3.connect(backup)) as target:
                origin.backup(target)
            expected=hashlib.sha256(backup.read_bytes()).hexdigest()
            command.upgrade(config,'head')
            with closing(sqlite3.connect(source)) as connection:
                self.assertNotIn('obsolete',[r[1] for r in connection.execute('PRAGMA table_info(business_sentinel)')])
                self.assertEqual(connection.execute('SELECT value FROM business_sentinel').fetchone(),('preserve-me',))
                self.assertEqual(connection.execute('SELECT cipher FROM encrypted_rows').fetchone(),(cipher,))
                self.assertEqual(connection.execute('SELECT * FROM workspace_identity').fetchone(),anchor)
                self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('new',))
            self.assertFalse(restored.exists())
            with closing(sqlite3.connect(backup)) as origin,closing(sqlite3.connect(restored)) as target: origin.backup(target)
            with closing(sqlite3.connect(restored)) as connection:
                self.assertEqual(connection.execute('SELECT * FROM business_sentinel').fetchone(),(1,'preserve-me','old-value'))
                self.assertEqual(connection.execute('SELECT cipher FROM encrypted_rows').fetchone(),(cipher,))
                self.assertEqual(connection.execute('SELECT * FROM workspace_identity').fetchone(),anchor)
                self.assertEqual(connection.execute('SELECT version_num FROM alembic_version').fetchone(),('old',))
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(),expected)
            self.assertTrue(source.exists())
