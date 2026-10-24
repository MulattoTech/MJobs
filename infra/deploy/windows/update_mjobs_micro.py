"""Fail-closed, no-agent Windows Docker Desktop updater for MJobs main.
Uses a hidden scheduled task; no active developer worktree is changed.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = Path(os.environ['LOCALAPPDATA']) / 'MJobsMicro'
BASE = Path(r'C:\Tools\Dev\MJobs-Micro')
OPS_PYTHON = Path(r'C:\Tools\Dev\MJobs\.venv\Scripts\python.exe')
STATE = HOME / 'state.json'
LOG = HOME / 'update.log'
REPOSITORY = 'MulattoTech/MJobs'
REQUIRED = ('Deployment / candidate', 'CI / required',
            'Integration / PostgreSQL and Redis', 'Security / secret scan')
WRITERS = ('api', 'worker', 'source-worker', 'document-worker', 'ai-worker',
           'company-discovery-worker', 'scheduler')
PORT = '18801'
PUBLIC_HOST = 'mjobs.m-techlabs.com'

def log(message):
    HOME.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    if LOG.exists() and LOG.stat().st_size > 256_000:
        LOG.replace(LOG.with_suffix('.old.log'))
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(f'{timestamp} {message}\n')

def command(args, *, cwd=None, env=None, timeout=120, label='command'):
    proc = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors='replace', timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if proc.returncode:
        raise RuntimeError(f'{label} failed (exit {proc.returncode})')
    return proc.stdout.strip()

def main_sha():
    value = command(['gh', 'api', f'repos/{REPOSITORY}/branches/main',
                     '--jq', '.commit.sha'], timeout=30, label='read main commit')
    if not re.fullmatch('[0-9a-f]{40}', value):
        raise RuntimeError('Main SHA is not a full lowercase commit')
    return value

def checks_pass(sha):
    data = json.loads(command(['gh', 'api', f'repos/{REPOSITORY}/commits/{sha}/check-runs?per_page=100'],
                              timeout=40, label='read independent checks'))
    if data.get('total_count', 0) > 100:
        raise RuntimeError('Checks are paginated; fail closed')
    rows = {}
    for entry in data.get('check_runs', []):
        name = entry.get('name')
        if name not in REQUIRED or entry.get('app', {}).get('slug') != 'github-actions':
            continue
        order = (entry.get('started_at') or '', entry.get('id') or 0)
        if name not in rows or order > rows[name][0]:
            rows[name] = (order, entry)
    return all(name in rows and rows[name][1].get('status') == 'completed'
               and rows[name][1].get('conclusion') == 'success' for name in REQUIRED)

def migration_digest(source):
    source = Path(source)
    paths = sorted((source / 'migrations').rglob('*.py')) + [source / 'alembic.ini']
    if not all(p.is_file() for p in paths) or len(paths) < 2:
        raise RuntimeError('Incomplete migration source')
    checksum = hashlib.sha256()
    for item in paths:
        checksum.update(item.relative_to(source).as_posix().encode('utf-8') + b'\0')
        checksum.update(hashlib.sha256(item.read_bytes()).digest())
    return checksum.hexdigest()

def compose(source, sha, image, *args, timeout=300):
    env = os.environ.copy()
    env.update(MJOBS_IMAGE=image, MJOBS_DEPLOY_SHA=sha, MJOBS_PORT=PORT,
               MJOBS_OPERATION_MODE='PERSONAL', MJOBS_COMPOSE_MOCK='0',
               MJOBS_PUBLIC_HOST=PUBLIC_HOST)
    return command(['docker', 'compose', '-f', str(Path(source) / 'compose.yml'),
                    '-f', str(Path(source) / 'compose.micro.yml'),
                    '-p', 'mjobs-micro', *args], cwd=source, env=env,
                   timeout=timeout, label='MJobs Compose')

def ready(expected, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/ready', timeout=4) as response:
                check = json.load(response)
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/__deployment', timeout=4) as response:
                version = json.load(response)
            if (check.get('status') == 'ready' and version.get('commit') == expected
                    and version.get('operation_mode') == 'PERSONAL'):
                return
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            pass
        time.sleep(3)
    raise RuntimeError('Readiness or deployed commit mismatch')

def atomic_state(data):
    temp = STATE.with_suffix('.new.json')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, STATE)

def run_update():
    if not (STATE.is_file() and BASE.is_dir() and OPS_PYTHON.is_file()):
        raise RuntimeError('MJobs micro baseline or backup runtime missing')
    previous = json.loads(STATE.read_text(encoding='utf-8'))
    old_sha = previous.get('sha', '')
    old_image = previous.get('image', '')
    old_source = Path(previous.get('source', ''))
    if (not re.fullmatch('[0-9a-f]{40}', old_sha)
            or old_image != 'mjobs-micro:sha-' + old_sha
            or not old_source.is_dir()):
        raise RuntimeError('Invalid protected deployment state')
    sha = main_sha()
    if sha == old_sha:
        return
    if not checks_pass(sha):
        log(f'No verified deployment candidate for main {sha[:12]}; retaining {old_sha[:12]}')
        return
    source = HOME / 'releases' / sha
    if source.exists():
        raise RuntimeError('Candidate directory already exists; requires reconciliation')
    source.parent.mkdir(parents=True, exist_ok=True)
    log(f'Cloning verified main candidate {sha[:12]}')
    command(['git', '-c', 'core.longpaths=true', 'clone', '--depth', '1',
             '--branch', 'main', f'https://github.com/{REPOSITORY}.git', str(source)],
            timeout=240, label='clone exact main')
    observed = command(['git', 'rev-parse', 'HEAD'], cwd=source, label='verify checkout')
    if observed != sha:
        raise RuntimeError('Main advanced during candidate clone; not deploying')
    if migration_digest(source) != migration_digest(old_source):
        raise RuntimeError('New migrations require offline guarded review; no automatic rollout')
    if ('MJOBS_PUBLIC_HOST' not in (source / 'compose.yml').read_text(encoding='utf-8')
            or 'MJOBS_IMAGE' not in (source / 'compose.yml').read_text(encoding='utf-8')):
        raise RuntimeError('Candidate lacks external-host deployment contract')
    for filename in ('.env', 'compose.micro.yml'):
        shutil.copy2(BASE / filename, source / filename)
    image = 'mjobs-micro:sha-' + sha
    log('Building full tested image from verified source (no code agents)')
    command(['docker', 'build', '-f', 'infra/docker/Dockerfile', '-t', image, '.'],
            cwd=source, timeout=4500, label='validated Docker image build')
    if main_sha() != sha or not checks_pass(sha):
        raise RuntimeError('Candidate was superseded or checks changed before deployment')

    stopped = False
    try:
        stopped = True
        compose(old_source, old_sha, old_image, 'stop', *WRITERS, timeout=180)
        archive = HOME / 'backups' / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                                        + '-' + old_sha[:12])
        archive.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(MJOBS_IMAGE=old_image, MJOBS_PORT=PORT,
                   MJOBS_OPERATION_MODE='PERSONAL', MJOBS_COMPOSE_MOCK='0')
        command([str(OPS_PYTHON), '-m', 'cli.local_backup', 'backup',
                 '--project', 'mjobs-micro', '--archive', str(archive)],
                cwd=old_source, env=env, timeout=900, label='quiescent backup')
        command([str(OPS_PYTHON), '-m', 'cli.local_backup', 'verify',
                 '--archive', str(archive)], cwd=old_source, timeout=180,
                label='verify backup')
        log('Protected database and document backup verified; switching MJobs only')
        compose(source, sha, image, 'up', '-d', '--no-build', timeout=600)
        ready(sha)
    except Exception:
        if stopped:
            log('Candidate failed after writers stopped: restoring previous image')
            try:
                compose(old_source, old_sha, old_image, 'up', '-d', '--no-build', timeout=300)
                ready(old_sha)
                log('Rollback healthy')
            except Exception:
                log('CRITICAL: prior-image rollback failed; preserve all Docker volumes and archives')
        raise
    atomic_state({'sha': sha, 'image': image, 'source': str(source),
                  'deployed_at': dt.datetime.now(dt.timezone.utc).isoformat()})
    log(f'SUCCESS: deployed validated main SHA {sha}')

def main():
    HOME.mkdir(parents=True, exist_ok=True)
    lock = HOME / 'update.lock'
    try:
        with lock.open('a+b') as fp:
            fp.seek(0, os.SEEK_END)
            if fp.tell() == 0:
                fp.write(b'0')
                fp.flush()
            fp.seek(0)
            try:
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return 0
            try:
                run_update()
                return 0
            finally:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception as error:
        log('BLOCKED: ' + (str(error) if isinstance(error, RuntimeError)
                           else type(error).__name__))
        return 1

if __name__ == '__main__':
    sys.exit(main())
