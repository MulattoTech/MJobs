"""Offline backup, one-use authorization, upgrade and validation for an EXISTING project.

Stop application writers yourself first. This command never stops/starts them,
restores a source database, accepts an old archive, or prints credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import time

from alembic.config import Config
from alembic.script import ScriptDirectory
from cli import local_backup as backup
from cli import migration_precondition as guard

VOLUME_PATH = '/var/lib/mjobs-migration-backups'


def inspect_service(project, service, environment):
    identifier=backup._container(project,service,environment)
    labels=json.loads(backup._run(['docker','inspect','--format','{{json .Config.Labels}}',identifier],
                                 action='Verify migration service labels'))
    if labels.get('com.docker.compose.service')!=service:
        raise guard.MigrationRefused('Wrong migration service identity')
    mounts=json.loads(backup._run(['docker','inspect','--format','{{json .Mounts}}',identifier],
                                 action='Verify migration service mounts'))
    volumes={}
    for destination,key in ((VOLUME_PATH,'migration-backups'),('/var/lib/postgresql/data','database')):
        if service!='postgres' and key=='database': continue
        found=[m for m in mounts if m.get('Destination')==destination]
        if len(found)!=1 or found[0].get('Type')!='volume' or not found[0].get('Name'):
            raise guard.MigrationRefused('Required project volume missing')
        volume=found[0]['Name']
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',volume):
            raise guard.MigrationRefused('Invalid project volume identity')
        own=json.loads(backup._run(['docker','volume','inspect','--format','{{json .Labels}}',volume],
                                  action='Verify migration volume ownership'))
        if own.get('com.docker.compose.project')!=project or own.get('com.docker.compose.volume')!=key:
            raise guard.MigrationRefused('Wrong project volume ownership')
        volumes[key]=volume
    image=backup._run(['docker','inspect','--format','{{.Image}}',identifier],
                      action='Verify migration image identity').decode().strip()
    return {'container':identifier,'image':image,'volumes':volumes}


def _state(project, environment, expected=None):
    # Every support exec is preceded by fresh project/service/volume inspection.
    def check():
        found=inspect_service(project,'postgres',environment)
        if expected is not None and found!=expected:
            raise guard.MigrationRefused('Postgres target changed')
    check()
    identity=json.loads(backup._sql(project,guard.IDENTITY_SQL,environment))
    check()
    heads=json.loads(backup._sql(project,
        "SELECT COALESCE(json_agg(version_num ORDER BY version_num),'[]'::json)::text FROM public.alembic_version",
        environment))
    check()
    rows=json.loads(backup._sql(project,
        "SELECT COALESCE(json_agg(t),'[]'::json)::text FROM "
        "(SELECT id,operation_mode,bound_at FROM public.workspace_identity LIMIT 2) t",environment))
    if (not isinstance(identity,dict) or set(identity)!={'system_identifier','database','database_oid'}
            or not isinstance(heads,list) or len(heads)!=1 or not isinstance(heads[0],str)):
        raise guard.MigrationRefused('Source database identity/revision unavailable')
    return {'identity':identity,'heads':heads,'anchor':guard.normalize_anchor(rows)}


def _quiescent(project, environment, mode):
    backup._require_quiescent(backup._service_states(project,environment),mode)


def _exec(project, environment, *arguments, expected=None, **kwargs):
    if inspect_service(project,'postgres',environment)!=expected:
        raise guard.MigrationRefused('Postgres target changed before exec')
    return backup._compose(project,'exec','-T','postgres',*arguments,environment=environment,**kwargs)


def run(project, archive):
    if not backup.PROJECT.fullmatch(project): raise guard.MigrationRefused('Invalid project')
    archive=archive.absolute()
    guard.require_private_parent(archive.parent)
    if archive.exists() or archive.is_symlink(): raise guard.MigrationRefused('Choose a new backup destination')
    environment=backup._config()  # explicit operator command, existing protected local configuration
    pg=inspect_service(project,'postgres',environment)
    migration=inspect_service(project,'migrate',environment)
    def execute(*arguments, **kwargs):
        return _exec(project,environment,*arguments,expected=pg,**kwargs)
    def state():
        return _state(project,environment,expected=pg)
    if pg['volumes']['migration-backups']!=migration['volumes']['migration-backups']:
        raise guard.MigrationRefused('Migration authorization volumes differ')
    backup.require_stopped_writers(backup._service_states(project,environment))
    source=state()
    mode=source['anchor']['operation_mode'] if source['anchor'] else None
    if mode not in ('PERSONAL','FIXTURE'):
        raise guard.MigrationRefused('Offline full backup requires a bound workspace')
    _quiescent(project,environment,mode)
    config=Config(str(guard.ROOT/'alembic.ini'))
    config.set_main_option('script_location',str(guard.ROOT/'migrations'))
    script=ScriptDirectory.from_config(config); heads=script.get_heads()
    if len(heads)!=1 or source['heads'][0] not in {r.revision for r in script.walk_revisions()}:
        raise guard.MigrationRefused('Unknown migration graph/source')
    if source['heads']==heads: raise guard.MigrationRefused('Database already at target; use normal provisioning')
    expected_code=guard.code_hash(); expected_schema=guard.digest(guard.required_signature())
    # Native tools live in the selected postgres service, never the app image.
    dump_version=execute('pg_dump','--version',action='Inspect native dump client')
    restore_version=execute('pg_restore','--version',action='Inspect native restore client')
    for value in (dump_version,restore_version):
        if not re.search(rb'\(PostgreSQL\) 17(?:\.|\s)',value):
            raise guard.MigrationRefused('PostgreSQL17 native backup tools required')
    backup.create_backup(project,archive)  # own guards remain unchanged; migrate must be stopped
    guard.require_private_parent(archive.parent)
    guard.require_private_archive(archive)
    def verified_archive():
        manifest=backup.verify_backup(archive)
        if (manifest['source_project']!=project or manifest['alembic_revision']!=source['heads'][0]
                or manifest['operation_mode']!=mode
                or manifest['compose_sha256']!=backup.checksum(guard.ROOT/'compose.yml')):
            raise guard.MigrationRefused('Backup does not belong to the reviewed source')
        return manifest
    verified_archive()
    if state()!=source: raise guard.MigrationRefused('Source changed during backup')
    _quiescent(project,environment,mode)
    # Snapshot verified bytes as restore_backup does: later source edits cannot swap the dump.
    with tempfile.TemporaryDirectory(prefix='.migration-snapshot-',dir=archive.parent) as directory:
        snapshot=Path(directory)
        dump=snapshot/'database.dump'
        expected_dump=verified_archive()['files']['database.dump']
        source_dump=archive/'database.dump'
        if backup._has_link_ancestor(source_dump): raise guard.MigrationRefused('Unsafe dump path')
        shutil.copyfile(source_dump,dump,follow_symlinks=False)
        if dump.is_symlink() or backup.checksum(dump)!=expected_dump:
            raise guard.MigrationRefused('Backup changed before migration snapshot')
        with dump.open('rb') as stream:
            listing=execute('pg_restore','--list',stdin=stream,timeout=120,
                          action='Validate native migration dump')
        if not listing.strip(): raise guard.MigrationRefused('Native dump inventory is empty')
        run_id=secrets.token_hex(16); nonce=secrets.token_hex(32); issued=int(time.time())
        receipt={'version':1,'run_id':run_id,'nonce_hash':hashlib.sha256(nonce.encode()).hexdigest(),
                 'issued_at':issued,'expires_at':issued+900,'source_identity':source['identity'],
                 'source_heads':source['heads'],'anchor':source['anchor'],'target_head':heads[0],
                 'code_hash':expected_code,'schema_hash':expected_schema,'dump_sha256':expected_dump,
                 'dump_size':dump.stat().st_size,'archive_sha256':backup.checksum(archive/'manifest.json'),
                 'project':project,'postgres_container':pg['container'],'database_volume':pg['volumes']['database']}
        receipt_path=snapshot/'receipt.pending'; receipt_path.write_text(json.dumps(receipt),encoding='utf8')
        destination=VOLUME_PATH+'/'+run_id
        execute('mkdir','-m','700','--',destination,action='Create private migration run')
        for local,name in ((dump,'database.dump'),(receipt_path,'receipt.pending')):
            current=inspect_service(project,'postgres',environment)
            if current!=pg: raise guard.MigrationRefused('Postgres identity changed')
            backup._run(['docker','cp',str(local),pg['container']+':'+destination+'/'+name],
                        timeout=600,action='Stage private migration authorization')
        execute('chown','10001:10001','--',destination,
              destination+'/database.dump',destination+'/receipt.pending',action='Own private migration run')
        execute('chmod','600','--',destination+'/database.dump',destination+'/receipt.pending',
              action='Protect migration authorization files')
        # Docker cp never conveys a checksum guarantee. Verify the staged copy using native tools.
        actual=execute('sha256sum','--',destination+'/database.dump',
                     action='Verify staged migration dump').decode().split()[0]
        if actual!=expected_dump: raise guard.MigrationRefused('Staged dump checksum mismatch')
        _quiescent(project,environment,mode)
        if (inspect_service(project,'postgres',environment)!=pg
                or inspect_service(project,'migrate',environment)!=migration
                or state()!=source or guard.code_hash()!=expected_code):
            raise guard.MigrationRefused('Migration source/target changed before launch')
        guard.require_private_parent(archive.parent)
        guard.require_private_archive(archive)
        if verified_archive()['files']['database.dump']!=expected_dump:
            raise guard.MigrationRefused('Backup changed before migration launch')
        execute('mv','-T','--',destination+'/receipt.pending',destination+'/receipt.ready',
              action='Publish one-use migration authorization')
        with tempfile.TemporaryFile(mode='w+b',dir=snapshot) as request:
            request.write(json.dumps({'nonce':nonce}).encode()); request.seek(0)
            # Last ownership check applies to the actual launch as well as support execs.
            if inspect_service(project,'migrate',environment)!=migration:
                raise guard.MigrationRefused('Migration service changed before launch')
            backup._compose(project,'run','--rm','--no-deps','--pull','never','-T','migrate',
                            'python','-m','cli.provision_database','--migration-run',run_id,
                            environment=environment,stdin=request,timeout=600,action='Run guarded database migration')
    return archive


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True)
    parser.add_argument('--archive',type=Path,required=True)
    args=parser.parse_args()
    try:
        run(args.project,args.archive)
    except Exception:
        # Tools/driver exceptions can include secrets. Keep diagnostics private.
        print('Guarded migration failed; keep the archive and services stopped for recovery',file=sys.stderr)
        return 1
    print('Guarded migration validated; backup retained and application services remain stopped')
    return 0


if __name__=='__main__': raise SystemExit(main())
