"""Versioned, fail-closed preconditions for the offline migration command.

The operator and database/Docker administrators are trusted. Receipts prevent
accidental stale/wrong-target migration; they are not administrator attestation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, LargeBinary, String, Text
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path('/var/lib/mjobs-migration-backups')
HEX = re.compile(r'^[0-9a-f]{64}$')
RUN = re.compile(r'^[0-9a-f]{32}$')
TRUSTED_INSTALLER_OWNER = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'


class MigrationRefused(ValueError):
    """Only fixed, nonsecret messages may cross the CLI boundary."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def strict_json(raw):
    def pairs(entries):
        result={}
        for key,value in entries:
            if key in result: raise MigrationRefused('Duplicate authorization field')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs)


def code_hash():
    paths = ['alembic.ini', 'compose.yml', 'requirements.lock',
             'packages/persistence/models.py', 'packages/persistence/resume_workspace.py',
             'cli/guarded_migration.py', 'cli/migration_precondition.py',
             'cli/provision_database.py', 'migrations/env.py']
    paths += sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'migrations/versions').glob('*.py'))
    return digest({name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in paths})


def type_signature(value):
    if isinstance(value, Text): return {'family': 'string', 'length': None}
    if isinstance(value, String): return {'family': 'string', 'length': value.length}
    if isinstance(value, Boolean): return {'family': 'boolean'}
    if isinstance(value, Integer): return {'family': 'integer'}
    if isinstance(value, Float): return {'family': 'float'}
    if isinstance(value, JSON): return {'family': 'json'}
    if isinstance(value, LargeBinary): return {'family': 'binary'}
    if isinstance(value, DateTime): return {'family': 'timestamp_tz' if value.timezone else 'timestamp'}
    raise MigrationRefused('Unsupported reviewed schema type')


def required_signature():
    from packages.persistence import models
    from packages.persistence import resume_workspace  # noqa: F401: explicit registration
    tables = {}
    for table in sorted(models.metadata.tables.values(), key=lambda row: row.name):
        if table.schema not in (None, 'public'):
            raise MigrationRefused('Unsupported reviewed namespace')
        fks = []
        for fk in table.foreign_key_constraints:
            fks.append({'columns': [e.parent.name for e in fk.elements],
                        'schema': fk.elements[0].column.table.schema or 'public',
                        'table': fk.elements[0].column.table.name,
                        'targets': [e.column.name for e in fk.elements],
                        'delete': fk.ondelete or 'NO ACTION', 'update': fk.onupdate or 'NO ACTION',
                        'match': fk.match or 'SIMPLE', 'deferrable': bool(fk.deferrable),
                        'deferred': fk.initially == 'DEFERRED', 'validated': True})
        tables[table.name] = {
            'columns': {c.name: dict(type_signature(c.type), nullable=c.nullable)
                        for c in sorted(table.columns, key=lambda c: c.name)},
            'pk': [c.name for c in table.primary_key.columns], 'pk_validated': True,
            'fks': sorted(fks, key=lambda item: json.dumps(item, sort_keys=True))}
    return {'policy_version': 1, 'tables': tables}


def validate_signature(required, actual):
    if actual.get('policy_version') != 1:
        raise MigrationRefused('Unknown schema policy')
    for table, expected in required['tables'].items():
        observed = actual['tables'].get(table)
        if not observed or observed['pk'] != expected['pk'] or not observed.get('pk_validated'):
            raise MigrationRefused('Required schema primary key mismatch')
        for name, column in expected['columns'].items():
            found = observed['columns'].get(name)
            if not found or found.get('family') != column['family'] or found.get('nullable') != column['nullable']:
                raise MigrationRefused('Required schema column mismatch')
            if column['family'] == 'string':
                need, have = column['length'], found['length']
                if have is not None and (need is None or have < need):
                    raise MigrationRefused('Required schema string capacity mismatch')
        if any(not fk.get('validated') for fk in observed['fks']):
            raise MigrationRefused('Unvalidated foreign key')
        if any(fk not in observed['fks'] for fk in expected['fks']):
            raise MigrationRefused('Required schema foreign key mismatch')


def normalize_anchor(rows):
    if not rows: return None
    if len(rows) != 1:
        raise MigrationRefused('Workspace identity is not a singleton')
    row = dict(rows[0])
    if (set(row) != {'id', 'operation_mode', 'bound_at'} or row['id'] != 'workspace'
            or row['operation_mode'] not in ('PERSONAL', 'FIXTURE')
            or not isinstance(row['bound_at'], str) or not 0 < len(row['bound_at']) <= 64):
        raise MigrationRefused('Invalid workspace identity')
    return row


def workspace_anchor(connection):
    if not inspect(connection).has_table('workspace_identity', schema='public'): return None
    return normalize_anchor(connection.execute(text(
        'SELECT id, operation_mode, bound_at FROM public.workspace_identity LIMIT 2')).mappings().all())


def current_heads(connection):
    if not inspect(connection).has_table('alembic_version', schema='public'): return []
    values = list(connection.execute(text('SELECT version_num FROM public.alembic_version')).scalars())
    if len(values) != 1 or not isinstance(values[0], str):
        raise MigrationRefused('Invalid migration revision shape')
    return values


IDENTITY_SQL = """SELECT json_build_object('system_identifier',
 (SELECT system_identifier::text FROM pg_control_system()), 'database', current_database(),
 'database_oid', (SELECT oid::bigint FROM pg_database WHERE datname=current_database()))"""


def database_identity(connection):
    value = connection.execute(text(IDENTITY_SQL)).scalar_one()
    if not isinstance(value, dict) or set(value) != {'system_identifier', 'database', 'database_oid'}:
        raise MigrationRefused('Database identity unavailable')
    return value


# System catalogs are trusted PostgreSQL-owned baseline, not an anti-admin-forgery boundary.
EMPTY_SQL = """SELECT
 current_setting('server_version_num')::int BETWEEN 170000 AND 179999
 AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN
 ('pg_catalog','information_schema','pg_toast','public'))
 AND EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='public'
 AND pg_get_userbyid(nspowner)='pg_database_owner'
 AND NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(nspacl,acldefault('n',nspowner))) a
 WHERE NOT ((a.grantee=0 AND a.privilege_type='USAGE' AND NOT a.is_grantable)
 OR (a.grantee=nspowner AND a.privilege_type IN ('USAGE','CREATE')))))
 AND (SELECT count(*) FROM pg_extension)=1
 AND EXISTS (SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
 WHERE e.extname='plpgsql' AND e.extversion='1.0' AND n.nspname='pg_catalog' AND NOT e.extrelocatable)
 AND NOT EXISTS (SELECT 1 FROM pg_language WHERE lanname NOT IN ('internal','c','sql','plpgsql'))
 AND NOT EXISTS (SELECT 1 FROM pg_event_trigger)
 AND NOT EXISTS (SELECT 1 FROM pg_foreign_data_wrapper)
 AND NOT EXISTS (SELECT 1 FROM pg_foreign_server)
 AND NOT EXISTS (SELECT 1 FROM pg_user_mapping)
 AND NOT EXISTS (SELECT 1 FROM pg_publication)
 AND NOT EXISTS (SELECT 1 FROM pg_subscription)
 AND NOT EXISTS (SELECT 1 FROM pg_default_acl)
 AND NOT EXISTS (SELECT 1 FROM pg_largeobject_metadata)
 AND NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public')
 AND NOT EXISTS (SELECT 1 FROM pg_proc c JOIN pg_namespace n ON n.oid=c.pronamespace WHERE n.nspname='public')
 AND NOT EXISTS (SELECT 1 FROM pg_type c JOIN pg_namespace n ON n.oid=c.typnamespace WHERE n.nspname='public')
 AND NOT EXISTS (SELECT 1 FROM pg_collation WHERE collnamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_conversion WHERE connamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_operator WHERE oprnamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_opclass WHERE opcnamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_opfamily WHERE opfnamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgnamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_ts_dict WHERE dictnamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_ts_parser WHERE prsnamespace='public'::regnamespace)
 AND NOT EXISTS (SELECT 1 FROM pg_ts_template WHERE tmplnamespace='public'::regnamespace)"""


def fresh_empty(connection):
    return connection.execute(text(EMPTY_SQL)).scalar_one() is True


def _actual_type(name, length):
    mapping = {'int4': 'integer', 'int8': 'integer', 'bool': 'boolean', 'float8': 'float',
               'json': 'json', 'jsonb': 'json', 'bytea': 'binary',
               'timestamp': 'timestamp', 'timestamptz': 'timestamp_tz'}
    if name in ('text', 'varchar'): return {'family': 'string', 'length': length}
    if name not in mapping: raise MigrationRefused('Unsupported database column type')
    return {'family': mapping[name]}


def observed_signature(connection, required):
    result = {'policy_version': 1, 'tables': {}}
    action = {'a': 'NO ACTION', 'r': 'RESTRICT', 'c': 'CASCADE', 'n': 'SET NULL', 'd': 'SET DEFAULT'}
    match = {'s': 'SIMPLE', 'f': 'FULL', 'p': 'PARTIAL'}
    for name in required['tables']:
        relation = connection.execute(text("SELECT c.oid,c.relkind FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=:name"), {'name': name}).one_or_none()
        if relation is None or relation.relkind not in ('r', 'p'):
            raise MigrationRefused('Required stored table missing')
        columns, numbers = {}, {}
        for row in connection.execute(text("SELECT a.attnum,a.attname,a.attnotnull,t.typname,t.typtype,"
                "a.atttypmod FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid "
                "WHERE a.attrelid=:oid AND a.attnum>0 AND NOT a.attisdropped"), {'oid': relation.oid}).mappings():
            numbers[row['attnum']] = row['attname']
            if row['attname'] not in required['tables'][name]['columns']: continue
            if row['typtype'] != 'b': raise MigrationRefused('User-defined column type is not allowed')
            length = row['atttypmod']-4 if row['typname']=='varchar' and row['atttypmod']>=4 else None
            columns[row['attname']] = dict(_actual_type(row['typname'], length), nullable=not row['attnotnull'])
        pk, pk_valid, fks = [], False, []
        constraints = connection.execute(text("SELECT c.*,n.nspname AS target_schema,t.relname AS target_table "
                "FROM pg_constraint c LEFT JOIN pg_class t ON t.oid=c.confrelid "
                "LEFT JOIN pg_namespace n ON n.oid=t.relnamespace "
                "WHERE c.conrelid=:oid AND c.contype IN ('p','f')"), {'oid': relation.oid}).mappings()
        for constraint in constraints:
            if constraint['contype']=='p':
                pk = [numbers[i] for i in constraint['conkey']]; pk_valid = constraint['convalidated']
                continue
            targets = dict(connection.execute(text('SELECT attnum,attname FROM pg_attribute WHERE attrelid=:oid'),
                                               {'oid': constraint['confrelid']}).all())
            fks.append({'columns': [numbers[i] for i in constraint['conkey']],
                        'schema': constraint['target_schema'], 'table': constraint['target_table'],
                        'targets': [targets[i] for i in constraint['confkey']],
                        'delete': action[constraint['confdeltype']], 'update': action[constraint['confupdtype']],
                        'match': match[constraint['confmatchtype']], 'deferrable': constraint['condeferrable'],
                        'deferred': constraint['condeferred'], 'validated': constraint['convalidated']})
        result['tables'][name] = {'columns': columns, 'pk': pk, 'pk_validated': pk_valid, 'fks': fks}
    if connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE contype='f' AND NOT convalidated)")).scalar_one():
        raise MigrationRefused('Unvalidated foreign key')
    return result


def validate_database(connection, target, anchor):
    connection.exec_driver_sql('SET TRANSACTION READ ONLY')
    connection.execute(text("SET LOCAL statement_timeout='15s'"))
    connection.execute(text("SET LOCAL lock_timeout='3s'"))
    if current_heads(connection) != [target]: raise MigrationRefused('Migration target not reached')
    required = required_signature()
    validate_signature(required, observed_signature(connection, required))
    if workspace_anchor(connection) != anchor: raise MigrationRefused('Workspace identity changed')
    connection.execute(text('SELECT 1')).scalar_one()
    quote = connection.dialect.identifier_preparer.quote
    for name, table in required['tables'].items():
        columns = ','.join(quote(c) for c in table['columns'])
        connection.exec_driver_sql(f'SELECT {columns} FROM ONLY public.{quote(name)} LIMIT 1').first()


def _link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _check_posix_parent(path):
    uid = os.geteuid()
    if path.stat().st_uid != uid or stat.S_IMODE(path.stat().st_mode)&0o077:
        raise MigrationRefused('Backup parent must be private and owned')
    for component in (path, *path.parents):
        info = component.stat()
        if info.st_uid not in (0, uid): raise MigrationRefused('Unsafe backup ancestor owner')
        if stat.S_IMODE(info.st_mode)&0o022 and not (info.st_uid==0 and info.st_mode&stat.S_ISVTX):
            raise MigrationRefused('Unsafe writable backup ancestor')
        try:
            # Linux POSIX ACLs grant access independently of simple mode tests.
            if {'system.posix_acl_access','system.posix_acl_default'} & set(os.listxattr(component)):
                raise MigrationRefused('Extended backup ACL requires explicit private ownership')
        except (AttributeError, OSError):
            raise MigrationRefused('Backup ACL inspection unavailable') from None


def _check_windows_parent(path):
    # Fixed script; the path travels as a process environment value, never code.
    script = r'''$ErrorActionPreference='Stop';
$u=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
$p=Get-Item -LiteralPath $env:MJOBS_PRIVATE_PARENT;
if ($p.PSDrive.Provider.Name -ne 'FileSystem' -or (Get-Volume -DriveLetter $p.PSDrive.Name).FileSystem -ne 'NTFS') {throw 'filesystem'};
$rows=@(); while($null -ne $p) {
 $a=Get-Acl -LiteralPath $p.FullName;
 $raw=[Security.AccessControl.RawSecurityDescriptor]::new($a.GetSecurityDescriptorBinaryForm(),0);
 if($null -eq $raw.DiscretionaryAcl){throw 'null dacl'};
 $rules=@($a.Access | ForEach-Object {@{sid=$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;
 allow=($_.AccessControlType -eq 'Allow');rights=[int64]$_.FileSystemRights;
 inherited=$_.IsInherited;inheritance=[int]$_.InheritanceFlags;propagation=[int]$_.PropagationFlags}});
 $rows+=@{owner=$a.GetOwner([Security.Principal.SecurityIdentifier]).Value;rules=$rules};
 if($p -is [IO.FileInfo]){$p=$p.Directory}else{$p=$p.Parent}
}; @{user=$u;rows=$rows}|ConvertTo-Json -Depth 6 -Compress'''
    environment = {k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','PATH','PATHEXT')}
    environment['MJOBS_PRIVATE_PARENT'] = str(path)
    try:
        process = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                                 env=environment, capture_output=True, timeout=10, check=True)
        if len(process.stdout)>65536: raise ValueError()
        data=json.loads(process.stdout); trusted={data['user'],'S-1-5-18','S-1-5-32-544'}
        ancestor_owners=trusted | {TRUSTED_INSTALLER_OWNER}
        if data['rows'][0]['owner'] != data['user']: raise ValueError()
        # Include unmapped GENERIC_ALL as well as concrete replacement rights.
        replacement = 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x10000000
        for index, row in enumerate(data['rows']):
            if row['owner'] not in (trusted if index==0 else ancestor_owners): raise ValueError()
            for ace in row['rules']:
                # Child-only grants do not apply to the ancestor itself. This
                # does not trust CREATOR OWNER or ignore applicable inherited ACEs.
                if index>0 and ace['propagation']&2: continue
                if ace['allow'] and ace['sid'] not in trusted and (index==0 or ace['rights']&replacement):
                    raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise MigrationRefused('Private NTFS owner/DACL could not be verified') from None


def require_private_parent(path):
    if not path.is_absolute() or not path.is_dir(): raise MigrationRefused('Private backup parent required')
    if any(_link(p) for p in (path,*path.parents)): raise MigrationRefused('Unsafe backup parent link')
    (_check_windows_parent if os.name=='nt' else _check_posix_parent)(path)


def require_private_archive(path):
    """NTFS traverse privilege makes a private parent alone insufficient."""
    require_private_parent(path)
    for node in path.rglob('*'):
        if _link(node) or not (node.is_file() or node.is_dir()):
            raise MigrationRefused('Unsafe private archive node')
        if os.name=='nt': _check_windows_parent(node)


def require_private_run(directory):
    if os.name!='posix' or os.geteuid()!=10001:
        raise MigrationRefused('Migration receipt requires the protected Linux runtime')
    for node, mode in ((directory,0o700),(directory/'database.dump',0o600),(directory/'receipt.ready',0o600)):
        info=node.lstat()
        if _link(node) or info.st_uid!=10001 or stat.S_IMODE(info.st_mode)!=mode:
            raise MigrationRefused('Unsafe migration receipt ownership')
        if (node==directory and not stat.S_ISDIR(info.st_mode)) or (node!=directory and not stat.S_ISREG(info.st_mode)):
            raise MigrationRefused('Unsafe migration receipt node')
    if any(_link(p) for p in directory.parents): raise MigrationRefused('Unsafe migration root')


def consume_receipt(root, run_id, nonce, expected, *, clock=None):
    if not isinstance(run_id,str) or not RUN.fullmatch(run_id) or not isinstance(nonce,str) or not HEX.fullmatch(nonce):
        raise MigrationRefused('Invalid migration authorization')
    directory=root/run_id
    try:
        require_private_run(directory)
        ready=directory/'receipt.ready'; consumed=directory/'receipt.consumed'
        if consumed.exists() or ready.stat().st_size>16384: raise ValueError()
        receipt=strict_json(ready.read_text(encoding='utf8'))
        keys=set(expected)|{'version','run_id','nonce_hash','issued_at','expires_at','dump_sha256','dump_size',
                            'archive_sha256','project','postgres_container','database_volume'}
        now=time.time() if clock is None else clock
        if (set(receipt)!=keys or type(receipt['version']) is not int or receipt['version']!=1 or receipt['run_id']!=run_id
                or type(receipt['issued_at']) is not int or type(receipt['expires_at']) is not int
                or not receipt['issued_at']<=now<receipt['expires_at']
                or receipt['expires_at']-receipt['issued_at']!=900
                or not hmac.compare_digest(receipt['nonce_hash'],hashlib.sha256(nonce.encode()).hexdigest())
                or any(receipt[k]!=v for k,v in expected.items())
                or not HEX.fullmatch(receipt['dump_sha256']) or not HEX.fullmatch(receipt['archive_sha256'])
                or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}',receipt['project'])
                or not HEX.fullmatch(receipt['postgres_container'])
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',receipt['database_volume'])
                or type(receipt['dump_size']) is not int or receipt['dump_size']<=0): raise ValueError()
        from cli.local_backup import checksum
        dump=directory/'database.dump'
        if dump.stat().st_size!=receipt['dump_size'] or checksum(dump)!=receipt['dump_sha256']: raise ValueError()
        os.rename(ready,consumed)  # second consumer finds no ready file; never rearm on failure
        if os.name=='posix':
            descriptor=os.open(directory,os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        return receipt
    except (OSError, ValueError, KeyError, TypeError):
        raise MigrationRefused('Migration authorization missing, stale, consumed or invalid') from None


def migrate_guarded(admin_url, config, *, run_id=None, nonce=None):
    script=ScriptDirectory.from_config(config)
    heads=script.get_heads()
    if len(heads)!=1: raise MigrationRefused('One reviewed migration head required')
    target=heads[0]; engine=create_engine(admin_url,connect_args={'connect_timeout':10})
    try:
        with engine.connect() as lock:
            if not lock.execute(text("SELECT pg_try_advisory_lock(hashtext('mjobs-migration-v1'),hashtext(current_database()))")).scalar_one():
                raise MigrationRefused('Another migration is active')
            lock.commit()
            source=current_heads(lock); anchor=workspace_anchor(lock)
            if source and (len(source)!=1 or script.get_revision(source[0]) is None
                           or source[0] not in {r.revision for r in script.walk_revisions()}):
                raise MigrationRefused('Unknown migration source')
            if run_id is not None:
                expected={'source_identity':database_identity(lock),'source_heads':source,'target_head':target,
                          'code_hash':code_hash(),'schema_hash':digest(required_signature()),'anchor':anchor}
                consume_receipt(RUN_ROOT,run_id,nonce,expected)
            elif source!=[target] and (source or not fresh_empty(lock)):
                raise MigrationRefused('Use the offline guarded migration command for this database')
            lock.commit()
            if source!=[target]: command.upgrade(config,'head')
            with engine.connect() as check:
                validate_database(check,target,anchor)
    finally:
        engine.dispose()
