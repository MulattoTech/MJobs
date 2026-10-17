"""Guarded migration policy: no Docker/credentials/services in these tests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import re
import sqlite3
from unittest.mock import patch, MagicMock
from contextlib import ExitStack
from cli import migration_precondition as guard


class MigrationPolicyTests(unittest.TestCase):
    def test_pg17_builtin_language_catalog_passes_actual_predicate_but_unknown_language_fails(self):
        # REL_17_STABLE/src/include/catalog/pg_language.dat: internal/c/sql;
        # the default plpgsql extension adds the fourth language. This tests
        # only the actual portable language predicate, NOT the full PG bootstrap.
        predicate=re.search(r'NOT EXISTS \(SELECT 1 FROM pg_language WHERE lanname NOT IN \([^)]*\)\)',
                            guard.EMPTY_SQL).group(0)
        connection=sqlite3.connect(':memory:')
        try:
            connection.execute('CREATE TABLE pg_language(lanname TEXT)')
            connection.executemany('INSERT INTO pg_language VALUES (?)',
                                   [(name,) for name in ('internal','c','sql','plpgsql')])
            self.assertEqual(connection.execute('SELECT '+predicate).fetchone(),(1,))
            connection.execute("INSERT INTO pg_language VALUES ('plpythonu')")
            self.assertEqual(connection.execute('SELECT '+predicate).fetchone(),(0,))
        finally: connection.close()

    def test_populated_pending_without_receipt_never_enters_upgrade(self):
        config=MagicMock(); script=MagicMock(); script.get_heads.return_value=['new']
        script.walk_revisions.return_value=[type('Revision',(),{'revision':'old'})()]
        engine=MagicMock()
        with patch.object(guard.ScriptDirectory,'from_config',return_value=script), \
                patch.object(guard,'create_engine',return_value=engine), \
                patch.object(guard,'current_heads',return_value=['old']), \
                patch.object(guard,'workspace_anchor',return_value=None), \
                patch.object(guard.command,'upgrade') as upgrade:
            with self.assertRaises(guard.MigrationRefused):
                guard.migrate_guarded('synthetic-never-connected',config)
            upgrade.assert_not_called()

    def test_already_head_and_empty_bootstrap_validate_before_success(self):
        for source in ([],['new']):
            engine=MagicMock(); script=MagicMock(); script.get_heads.return_value=['new']
            script.walk_revisions.return_value=[type('Revision',(),{'revision':'new'})()]
            with patch.object(guard.ScriptDirectory,'from_config',return_value=script), \
                    patch.object(guard,'create_engine',return_value=engine), \
                    patch.object(guard,'current_heads',return_value=source), \
                    patch.object(guard,'workspace_anchor',return_value=None), \
                    patch.object(guard,'fresh_empty',return_value=True), \
                    patch.object(guard,'validate_database') as validate, \
                    patch.object(guard.command,'upgrade') as upgrade:
                guard.migrate_guarded('synthetic-never-connected',MagicMock())
                self.assertEqual(upgrade.call_count,0 if source else 1)
                validate.assert_called_once()

    def test_schema_extras_allowed_but_required_string_cannot_narrow(self):
        required={'policy_version':1,'tables':{'sample':{'columns':{'id':{'family':'string','length':128,'nullable':False}},
                  'pk':['id'],'pk_validated':True,'fks':[]}}}
        actual=copy.deepcopy(required)
        actual['tables']['obsolete']={'columns':{},'pk':[],'fks':[]}
        actual['tables']['sample']['columns']['extra']={'family':'string','length':None,'nullable':True}
        actual['tables']['sample']['columns']['id']['length']=None
        guard.validate_signature(required,actual)
        actual['tables']['sample']['columns']['id']['length']=127
        with self.assertRaises(guard.MigrationRefused): guard.validate_signature(required,actual)

    def test_wrong_workspace_blocks_postvalidation(self):
        connection=MagicMock(); expected=guard.required_signature()
        with patch.object(guard,'current_heads',return_value=['target']), \
                patch.object(guard,'observed_signature',return_value=expected), \
                patch.object(guard,'workspace_anchor',return_value={'id':'workspace','operation_mode':'FIXTURE','bound_at':'changed'}):
            with self.assertRaisesRegex(guard.MigrationRefused,'Workspace'):
                guard.validate_database(connection,'target',None)

    def test_required_signature_registers_resume_workspace_and_is_deterministic(self):
        first = guard.required_signature()
        self.assertIn('resume_workspace_revisions', first['tables'])
        self.assertEqual(first, guard.required_signature())
        self.assertEqual(first['policy_version'], 1)

    def test_metadata_type_normalization_is_explicit(self):
        from sqlalchemy import String, Text, Integer, Float, Boolean, JSON, LargeBinary, DateTime, Numeric
        for value, family in ((String(128),'string'),(Text(),'string'),(Integer(),'integer'),
                              (Float(),'float'),(Boolean(),'boolean'),(JSON(),'json'),
                              (LargeBinary(),'binary'),(DateTime(timezone=True),'timestamp_tz')):
            with self.subTest(family=family):
                self.assertEqual(guard.type_signature(value)['family'], family)
        with self.assertRaises(guard.MigrationRefused):
            guard.type_signature(Numeric())

    def test_schema_rejects_changed_nullability_pk_and_unvalidated_fk(self):
        required = guard.required_signature()
        guard.validate_signature(required, copy.deepcopy(required))
        table = next(name for name, data in required['tables'].items() if data['fks'])
        for change in ('nullable','pk','fk'):
            observed = copy.deepcopy(required)
            data = observed['tables'][table]
            if change == 'nullable':
                column = next(iter(data['columns'].values()))
                column['nullable'] = not column['nullable']
            elif change == 'pk': data['pk'] = []
            else: data['fks'][0]['validated'] = False
            with self.subTest(change=change), self.assertRaises(guard.MigrationRefused):
                guard.validate_signature(required, observed)

    def test_workspace_anchor_is_only_absence_or_valid_exact_singleton(self):
        row = {'id':'workspace','operation_mode':'PERSONAL','bound_at':'2026-10-17T00:00:00Z'}
        self.assertIsNone(guard.normalize_anchor([]))
        self.assertEqual(guard.normalize_anchor([row]),row)
        for rows in ([row,row],[dict(row,id='other')],[dict(row,operation_mode='UNKNOWN')]):
            with self.assertRaises(guard.MigrationRefused): guard.normalize_anchor(rows)

    def test_receipt_is_target_bound_and_consumed_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run_id = 'a'*32; nonce = 'b'*64
            child=root/run_id; child.mkdir(); data=b'fixed synthetic dump'
            (child/'database.dump').write_bytes(data)
            expected={'source_identity':{'system_identifier':'123','database':'mjobs_test','database_oid':42},
                      'source_heads':['old'],'target_head':'new','code_hash':'c'*64,
                      'schema_hash':'d'*64,'anchor':None}
            receipt=dict(expected,version=1,run_id=run_id,nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                         issued_at=1000,expires_at=1900,dump_sha256=hashlib.sha256(data).hexdigest(),
                         dump_size=len(data),archive_sha256='e'*64,project='synthetic',
                         postgres_container='f'*64,database_volume='synthetic_database')
            (child/'receipt.ready').write_text(json.dumps(receipt))
            with patch.object(guard,'require_private_run'):
                guard.consume_receipt(root,run_id,nonce,expected,clock=1001)
                self.assertTrue((child/'receipt.consumed').is_file())
                with self.assertRaises(guard.MigrationRefused):
                    guard.consume_receipt(root,run_id,nonce,expected,clock=1001)

    def test_parent_permission_check_never_mutates_acl(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(guard,'_check_posix_parent') as posix, patch.object(guard,'_check_windows_parent') as windows:
                guard.require_private_parent(Path(directory))
                self.assertEqual(posix.call_count+windows.call_count,1)

    def test_windows_acl_unknown_allow_or_owner_is_refused_without_changes(self):
        safe={'user':'S-1-5-21-synthetic','rows':[{'owner':'S-1-5-21-synthetic','rules':[
            {'sid':'S-1-5-18','allow':True,'rights':0x1f01ff,'inherited':False,'inheritance':0,'propagation':0}]}]}
        with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(safe).encode())):
            guard._check_windows_parent(Path('synthetic'))
        for changed in ('owner','allow'):
            value=copy.deepcopy(safe)
            if changed=='owner': value['rows'][0]['owner']='unknown'
            else: value['rows'][0]['rules'].append({'sid':'unknown','allow':True,'rights':1,
                                                  'inherited':True,'inheritance':0,'propagation':0})
            with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(value).encode())), \
                    self.assertRaises(guard.MigrationRefused):
                guard._check_windows_parent(Path('synthetic'))

    def test_ancestor_inherit_only_is_not_direct_but_applicable_inherited_delete_child_refuses(self):
        data={'user':'S-1-5-21-synthetic','rows':[
            {'owner':'S-1-5-21-synthetic','rules':[]},
            {'owner':'S-1-5-18','rules':[{'sid':'S-1-3-0','allow':True,'rights':0x1f01ff,
                                       'inherited':False,'inheritance':3,'propagation':2}]}]}
        with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(data).encode())):
            guard._check_windows_parent(Path('synthetic'))

        for replacement in (0x40,0x10000000):  # DELETE_CHILD and an unmapped GENERIC_ALL ACE
            data['rows'][1]['rules'][0].update(sid='unknown',inherited=True,propagation=0,rights=replacement)
            with self.subTest(replacement=replacement), \
                    patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(data).encode())), \
                    self.assertRaises(guard.MigrationRefused):
                guard._check_windows_parent(Path('synthetic'))
        # The actual private parent stays strict, including child-only grants.
        data['rows'][0]['rules']=[{'sid':'S-1-3-0','allow':True,'rights':1,
                                  'inherited':False,'inheritance':3,'propagation':2}]
        data['rows'][1]['rules']=[]
        with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(data).encode())), \
                self.assertRaises(guard.MigrationRefused):
            guard._check_windows_parent(Path('synthetic'))

    def test_windows_owner_rights_current_owned_leaf_is_private(self):
        # Windows tempfile creates a protected OWNER RIGHTS + SYSTEM/Admin DACL.
        data={'user':'S-1-5-21-synthetic','rows':[{'owner':'S-1-5-21-synthetic','rules':[
            {'sid':sid,'allow':True,'rights':0x1f01ff,'inherited':False,'inheritance':3,'propagation':0}
            for sid in ('S-1-3-4','S-1-5-18','S-1-5-32-544')]}]}
        with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(data).encode())):
            guard._check_windows_parent(Path('synthetic'))

    def test_windows_owner_rights_on_current_owned_archive_ancestor_is_private(self):
        # A descendant check must resolve the archive root's owner alias too.
        data={'user':'S-1-5-21-synthetic','rows':[
            {'owner':'S-1-5-21-synthetic','rules':[]},
            {'owner':'S-1-5-21-synthetic','rules':[
                {'sid':'S-1-3-4','allow':True,'rights':0x1f01ff,
                 'inherited':False,'inheritance':3,'propagation':0}]}]}
        with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(data).encode())):
            guard._check_windows_parent(Path('synthetic'))

    def test_windows_inherited_owner_rights_resolves_only_current_node_owner(self):
        data={'user':'S-1-5-21-synthetic','rows':[
            {'owner':'S-1-5-21-synthetic','rules':[
                {'sid':'S-1-3-4','allow':True,'rights':0x1f01ff,
                 'inherited':True,'inheritance':3,'propagation':0}]},
            {'owner':'S-1-5-21-synthetic','rules':[]}]}
        with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(data).encode())):
            guard._check_windows_parent(Path('synthetic'))
        for change in ('noncurrent_leaf_owner','noncurrent_ancestor_owner','creator_owner','unrelated_allow'):
            changed=copy.deepcopy(data)
            if change=='noncurrent_leaf_owner': changed['rows'][0]['owner']='S-1-5-18'
            elif change=='noncurrent_ancestor_owner':
                changed['rows'][1]={'owner':'S-1-5-18','rules':copy.deepcopy(data['rows'][0]['rules'])}
            elif change=='creator_owner': changed['rows'][0]['rules'][0]['sid']='S-1-3-0'
            else: changed['rows'][0]['rules'].append({'sid':'unrelated','allow':True,'rights':1,
                                                     'inherited':False,'inheritance':0,'propagation':0})
            with self.subTest(change=change), \
                    patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(changed).encode())), \
                    self.assertRaises(guard.MigrationRefused):
                guard._check_windows_parent(Path('synthetic'))

    def test_exact_trustedinstaller_is_ancestor_owner_only_never_private_leaf_allow(self):
        ti='S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
        data={'user':'S-1-5-21-synthetic','rows':[
            {'owner':'S-1-5-21-synthetic','rules':[]},{'owner':ti,'rules':[]}]}
        with patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(data).encode())):
            guard._check_windows_parent(Path('synthetic'))
        for change in ('arbitrary_service_owner','ti_leaf_owner','ti_leaf_allow'):
            changed=copy.deepcopy(data)
            if change=='arbitrary_service_owner': changed['rows'][1]['owner']='S-1-5-80-1-2-3-4-5'
            elif change=='ti_leaf_owner': changed['rows'][0]['owner']=ti
            else: changed['rows'][0]['rules']=[{'sid':ti,'allow':True,'rights':1,
                                              'inherited':False,'inheritance':0,'propagation':0}]
            with self.subTest(change=change), \
                    patch.object(guard.subprocess,'run',return_value=MagicMock(stdout=json.dumps(changed).encode())), \
                    self.assertRaises(guard.MigrationRefused):
                guard._check_windows_parent(Path('synthetic'))

    def test_duplicate_authorization_fields_are_not_last_value_wins(self):
        with self.assertRaises(guard.MigrationRefused):
            guard.strict_json('{"nonce":"old","nonce":"new"}')

    def test_missing_or_wrong_receipt_cannot_authorize_upgrade(self):
        for run_id,nonce in (('../escape','b'*64),('a'*32,'bad'),('a'*32,'b'*64)):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(guard.MigrationRefused):
                    guard.consume_receipt(Path(directory),run_id,nonce,{})

    def test_corrupt_expired_wrong_target_and_nonce_receipts_remain_unconsumed(self):
        mutations=({'target_head':'wrong'},{'expires_at':1001},{'nonce_hash':'f'*64},{'dump_sha256':'f'*64},
                   {'source_heads':['wrong']},{'version':True},{'dump_size':999})
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root=Path(directory); run_id='a'*32; nonce='b'*64
                child=root/run_id; child.mkdir(); data=b'synthetic dump'; (child/'database.dump').write_bytes(data)
                expected={'source_identity':{'system_identifier':'123','database':'mjobs_test','database_oid':42},
                          'source_heads':['old'],'target_head':'new','code_hash':'c'*64,'schema_hash':'d'*64,'anchor':None}
                value=dict(expected,version=1,run_id=run_id,nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                    issued_at=1000,expires_at=1900,dump_sha256=hashlib.sha256(data).hexdigest(),dump_size=len(data),
                    archive_sha256='e'*64,project='synthetic',postgres_container='f'*64,database_volume='synthetic_database')
                value.update(mutation); (child/'receipt.ready').write_text(json.dumps(value))
                with patch.object(guard,'require_private_run'), self.assertRaises(guard.MigrationRefused):
                    guard.consume_receipt(root,run_id,nonce,expected,clock=1001)
                self.assertTrue((child/'receipt.ready').exists()); self.assertFalse((child/'receipt.consumed').exists())

    def test_compose_volume_is_confined_to_postgres_and_migrate(self):
        import yaml
        root=Path(__file__).resolve().parents[2]
        config=yaml.safe_load((root/'compose.yml').read_text())
        consumers={name for name,service in config['services'].items()
                   if any(str(v).startswith('migration-backups:') for v in service.get('volumes',[]))}
        self.assertEqual(consumers,{'postgres','migrate'})
        self.assertNotIn('docker.sock',(root/'compose.yml').read_text())


class OfflineSequenceTests(unittest.TestCase):
    """Real host driver ordering; existing backup archive verifier, no Docker calls."""
    def exercise(self, root, *, failure=None):
        from cli import guarded_migration as host
        from tests.unit.test_local_backup import LocalBackupTests
        events=[]; archive=root/'archive'; source={'identity':{'database':'mjobs_test','database_oid':42,
            'system_identifier':'123'},'heads':['old'],
            'anchor':{'id':'workspace','operation_mode':'PERSONAL','bound_at':'2026-10-17T00:00:00Z'}}
        pg={'container':'f'*64,'image':'sha256:'+'a'*64,
            'volumes':{'migration-backups':'synthetic_migration-backups','database':'synthetic_database'}}
        migration=dict(pg,container='e'*64,volumes={'migration-backups':'synthetic_migration-backups'})
        def create(project, destination):
            events.append('backup')
            if failure=='backup': raise host.backup.BackupError('synthetic failed dump')
            destination.mkdir(); manifest=LocalBackupTests.archive_fixture(destination)
            manifest.update(source_project='synthetic',alembic_revision='old')
            if failure=='wrong_project': manifest['source_project']='different'
            (destination/'manifest.json').write_text(json.dumps(manifest))
            return manifest
        def execute(*args,**kwargs):
            action=kwargs['action']; events.append(action)
            if 'version' in args: return b'pg_dump (PostgreSQL) 17.6\n'
            if '--version' in args: return b'pg_restore (PostgreSQL) 17.6\n'
            if '--list' in args:
                if failure=='list': raise host.backup.BackupError('synthetic invalid native dump')
                return b'; synthetic list structure\n'
            if args[2:3]==('sha256sum',) or 'sha256sum' in args:
                return (host.backup.checksum(archive/'database.dump')+'  database.dump\n').encode()
            return b''
        def launch(*args,**kwargs):
            events.append('launch')
            self.assertIn('--pull',args); self.assertIn('never',args); self.assertNotIn('--build',args)
            self.assertEqual(set(json.loads(kwargs['stdin'].read())),{'nonce'})
            return b''
        states={name:'exited' for name in host.backup.WRITERS}; states['postgres']='running'
        script=MagicMock(); script.get_heads.return_value=['new']
        script.walk_revisions.return_value=[type('Revision',(),{'revision':'old'})()]
        with ExitStack() as stack:
            for owner,name,value in ((host.guard,'require_private_parent',MagicMock()),
                    (host.guard,'require_private_archive',MagicMock()),(host.guard,'code_hash',lambda:'c'*64),
                    (host,'inspect_service',lambda p,s,e:pg if s=='postgres' else migration),
                    (host,'_state',lambda *a,**k:copy.deepcopy(source)),
                    (host,'_exec',execute),(host.backup,'_config',lambda:{}),
                    (host.backup,'_service_states',lambda *a:dict(states)),
                    (host.backup,'create_backup',create),(host.backup,'_run',lambda *a,**k:b''),
                    (host.backup,'_compose',launch)):
                stack.enter_context(patch.object(owner,name,value))
            stack.enter_context(patch.object(host.ScriptDirectory,'from_config',return_value=script))
            if failure:
                with self.assertRaises((host.guard.MigrationRefused,host.backup.BackupError)):
                    host.run('synthetic',archive)
            else: self.assertEqual(host.run('synthetic',archive),archive)
        return events,archive

    def test_missing_failed_or_wrong_source_backup_never_launches_migration(self):
        for failure in ('backup','list','wrong_project'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                events,archive=self.exercise(Path(directory),failure=failure)
                self.assertNotIn('launch',events)
                if failure!='backup': self.assertTrue((archive/'database.dump').is_file())

    def test_backup_native_verification_publish_then_launch_and_archive_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            events,archive=self.exercise(Path(directory))
            self.assertLess(events.index('backup'),events.index('Validate native migration dump'))
            self.assertLess(events.index('Verify staged migration dump'),events.index('Publish one-use migration authorization'))
            self.assertLess(events.index('Publish one-use migration authorization'),events.index('launch'))
            self.assertTrue((archive/'database.dump').is_file())


if __name__=='__main__': unittest.main()
