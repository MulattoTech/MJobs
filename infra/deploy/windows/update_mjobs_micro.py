"""Fail-closed, no-agent Windows Docker Desktop updater for MJobs main.
Uses a hidden scheduled task; no active developer worktree is changed.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
if os.name == 'nt':
    import msvcrt
else:
    msvcrt = None
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / '.local' / 'share')) / 'MJobsMicro'
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
MAX_BACKUP_DIAGNOSTIC_BYTES = 8192
BACKUP_REFUSALS = {
    'BACKUP_POSTGRES_NOT_RUNNING': ('PostgreSQL must be running for a logical backup',),
    'BACKUP_ENV_MISSING': ('Project-local .env is missing; run python -m cli.init_env first',),
    'BACKUP_DESTINATION_EXISTS': (
        'Backup destination already exists; choose a new path',
        'Backup destination appeared during capture; refusing overwrite',
    ),
    'BACKUP_DESTINATION_UNSAFE': ('Backup destination contains an unsafe link',),
    'BACKUP_DESTINATION_PARENT_MISSING': ('Backup parent directory does not exist',),
    'BACKUP_RUNTIME_CONFIGURATION_INVALID': (
        'Backup runtime configuration is invalid',
        'Backup runtime configuration does not match its workspace mode',
        'Backup runtime configuration contains unsupported settings',
        'Backup runtime configuration has an invalid mock mode',
        'Backup runtime configuration contains an invalid setting',
        'API runtime configuration could not be safely captured',
        'Backup runtime configuration exceeds its size limit',
        'Backup runtime configuration is not valid JSON',
    ),
    'BACKUP_DATABASE_METADATA_INVALID': (
        'Database backup metadata is missing or ambiguous',
        'Database operation mode is not bound',
    ),
    'BACKUP_CONTAINER_OWNERSHIP_MISMATCH': ('Container belongs to another project',),
    'BACKUP_ARCHIVE_INVALID': (
        'Backup inventory could not be read',
        'Backup directory is a symbolic link',
        'Backup directory is missing or is a symbolic link',
        'Backup document directory is missing or unsafe',
        'Backup manifest is missing or is a symbolic link',
        'Backup manifest is not valid JSON',
        'Unsupported backup manifest version',
        'Backup contains unlisted or missing files',
        'Backup configuration snapshot is incomplete',
        'Archived Compose source does not match its recorded hash',
    ),
}
BACKUP_ACTIONS = {
    'Inspect project services': 'INSPECT_SERVICES',
    'Locate api container': 'LOCATE_API',
    'Locate mock container': 'LOCATE_MOCK',
    'Check container ownership': 'CHECK_OWNERSHIP',
    'Read database backup metadata': 'READ_DATABASE_METADATA',
    'Dump PostgreSQL': 'DATABASE_DUMP',
    'Copy project documents': 'COPY_DOCUMENTS',
    'Inspect API runtime configuration': 'INSPECT_RUNTIME_CONFIGURATION',
    'Copy fixture provider receipts': 'COPY_FIXTURE_RECEIPTS',
}

def log(message):
    HOME.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    if LOG.exists() and LOG.stat().st_size > 256_000:
        LOG.replace(LOG.with_suffix('.old.log'))
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(f'{timestamp} {message}\n')

def backup_failure_code(stderr):
    """Recognize fixed legacy CLI refusals; never retain child output or paths."""
    fallback = 'BACKUP_FAILURE_UNCLASSIFIED'
    if (not isinstance(stderr, str) or len(stderr) > MAX_BACKUP_DIAGNOSTIC_BYTES
            or len(stderr.encode('utf-8', errors='replace')) > MAX_BACKUP_DIAGNOSTIC_BYTES):
        return fallback
    # Accept one normal CLI line ending, but no extra lines, prefixes or suffixes.
    message = stderr.removesuffix('\n').removesuffix('\r')
    prefix = 'Backup refused: '
    if not message.startswith(prefix) or '\n' in message or '\r' in message:
        return fallback
    detail = message[len(prefix):]
    for code, messages in BACKUP_REFUSALS.items():
        if detail in messages:
            return code
    for service in (*WRITERS, 'migrate'):
        if detail == f'Stop {service} before backing up the database and documents':
            return 'BACKUP_WRITER_' + service.upper().replace('-', '_') + '_NOT_STOPPED'
    if detail == 'Stop mock before backing up fixture receipts':
        return 'BACKUP_FIXTURE_NOT_STOPPED'
    for service in ('api', 'mock'):
        if detail == f'Exactly one {service} container is required':
            return 'BACKUP_' + service.upper() + '_CONTAINER_AMBIGUOUS'
    for action, code in BACKUP_ACTIONS.items():
        if detail == f'{action} could not complete; inspect Docker state':
            return f'BACKUP_{code}_UNAVAILABLE'
        if detail == f'{action} exceeded the captured-output limit':
            return f'BACKUP_{code}_OUTPUT_LIMIT'
        if re.fullmatch(re.escape(action) + r' failed with exit code -?[0-9]{1,10}; inspect Docker logs', detail):
            return f'BACKUP_{code}_FAILED'
    return fallback

def command(args, *, cwd=None, env=None, timeout=120, label='command', backup_diagnostics=False):
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, errors='replace', timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        if backup_diagnostics:
            raise RuntimeError(f'{label} timed out [BACKUP_COMMAND_TIMEOUT]') from None
        raise
    if proc.returncode:
        diagnostic = f' [{backup_failure_code(proc.stderr)}]' if backup_diagnostics else ''
        raise RuntimeError(f'{label} failed (exit {proc.returncode})' + diagnostic)
    return proc.stdout.strip()

def main_sha():
    value = command(['gh', 'api', f'repos/{REPOSITORY}/branches/main',
                     '--jq', '.commit.sha'], timeout=30, label='read main commit')
    if not re.fullmatch('[0-9a-f]{40}', value):
        raise RuntimeError('Main SHA is not a full lowercase commit')
    return value

def check_runs(sha):
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
        raise RuntimeError('Invalid commit for independent checks')
    pages = json.loads(command(
        ['gh', 'api', '--paginate', '--slurp',
         f'repos/{REPOSITORY}/commits/{sha}/check-runs?filter=all&per_page=100'],
        timeout=120, label='read all independent checks'))
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
        raise RuntimeError('Invalid paginated check response; fail closed')
    expected = pages[0].get('total_count')
    if type(expected) is not int or expected < 0:
        raise RuntimeError('Invalid check count; fail closed')
    runs = {}
    for page in pages:
        if (not isinstance(page, dict)
                or type(page.get('total_count')) is not int
                or page['total_count'] != expected
                or not isinstance(page.get('check_runs'), list)):
            raise RuntimeError('Check pages changed or are incomplete; fail closed')
        for entry in page['check_runs']:
            if (not isinstance(entry, dict)
                    or type(entry.get('id')) is not int or entry['id'] <= 0
                    or entry.get('head_sha') != sha):
                raise RuntimeError('Invalid check identity or commit; fail closed')
            if entry['id'] in runs:
                raise RuntimeError('Duplicate check across pages; retry next cycle')
            runs[entry['id']] = entry
    if len(runs) != expected:
        raise RuntimeError('Incomplete check pagination; fail closed')
    return list(runs.values())

def checks_pass(sha):
    rows = {}
    for entry in check_runs(sha):
        name = entry.get('name')
        app = entry.get('app')
        if (name not in REQUIRED or not isinstance(app, dict)
                or app.get('slug') != 'github-actions'):
            continue
        # A newly queued run may have no started_at. Its newer ID must still
        # supersede an older success, or an in-progress rerun could be bypassed.
        if name not in rows or entry['id'] > rows[name]['id']:
            rows[name] = entry
    return all(name in rows and rows[name].get('status') == 'completed'
               and rows[name].get('conclusion') == 'success' for name in REQUIRED)

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

def require_plain_path(path):
    """Reject redirected existing components, including Windows junctions."""
    path = Path(path).absolute()
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise RuntimeError('Redirected deployment path; requires reconciliation')

def candidate_git_environment():
    """Use identical checkout and verification rules regardless of host Git settings."""
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
    return env

def verify_candidate(source, sha):
    """Reuse only an independent, unchanged clone at the expected release path."""
    source = Path(source).absolute()
    if (not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha)
            or source != (HOME / 'releases' / sha).absolute()):
        raise RuntimeError('Unexpected candidate path or commit')
    require_plain_path(source)
    if not source.is_dir() or not (source / '.git').is_dir():
        raise RuntimeError('Candidate is not an independent clone; requires reconciliation')

    def walk_error(error):
        raise RuntimeError('Cannot inspect candidate paths') from error

    for root, directories, files in os.walk(source, followlinks=False, onerror=walk_error):
        for name in directories + files:
            item = Path(root) / name
            info = item.lstat()
            if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise RuntimeError('Redirected or special candidate entry; requires reconciliation')

    # Ignore ambient Git overrides; do not execute cached local hooks or filters.
    env = candidate_git_environment()

    def git(*args):
        return command(['git', '--no-replace-objects', '-c', 'core.fsmonitor=false',
                        *args], cwd=source, env=env, label='verify release clone')

    allowed_config = {'core.repositoryformatversion', 'core.filemode', 'core.bare',
                      'core.logallrefupdates', 'core.symlinks', 'core.ignorecase',
                      'core.longpaths', 'remote.origin.url', 'remote.origin.fetch',
                      'branch.main.remote', 'branch.main.merge'}
    keys = git('config', '--local', '--no-includes', '--name-only', '--list').splitlines()
    if any(key.lower() not in allowed_config for key in keys):
        raise RuntimeError('Unexpected candidate Git configuration; requires reconciliation')
    if (source / '.git' / 'objects' / 'info' / 'alternates').exists():
        raise RuntimeError('Candidate uses external Git objects; requires reconciliation')
    if (Path(git('rev-parse', '--show-toplevel')).absolute() != source
            or Path(git('rev-parse', '--absolute-git-dir')).absolute() != source / '.git'
            or (source / git('rev-parse', '--git-common-dir')).absolute() != source / '.git'
            or git('remote', 'get-url', '--all', 'origin') != f'https://github.com/{REPOSITORY}.git'
            or git('rev-parse', 'HEAD') != sha):
        raise RuntimeError('Candidate provenance or commit mismatch; requires reconciliation')
    if git('status', '--porcelain=v1', '--untracked-files=no', '--ignore-submodules=none'):
        raise RuntimeError('Candidate tracked files changed; requires reconciliation')
    if any(entry and not entry.startswith('H ')
           for entry in git('ls-files', '-v', '-z').split('\0')):
        raise RuntimeError('Candidate index hides tracked changes; requires reconciliation')
    overlays = {'.env', 'compose.micro.yml'}
    if git('ls-files', '-z', '--', *sorted(overlays)):
        raise RuntimeError('Candidate tracks a private deployment overlay')
    # The status tag preserves leading whitespace in names through command().strip().
    extras = (git('ls-files', '-t', '--others', '--exclude-standard', '-z') + '\0'
              + git('ls-files', '-t', '--others', '--ignored', '--exclude-standard', '-z'))
    if any(entry and (not entry.startswith('? ') or entry[2:] not in overlays)
           for entry in extras.split('\0')):
        raise RuntimeError('Unknown candidate files; requires reconciliation')
    if any((source / name).exists() and not (source / name).is_file() for name in overlays):
        raise RuntimeError('Invalid candidate overlay; requires reconciliation')

def prepare_candidate(sha):
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
        raise RuntimeError('Invalid candidate commit')
    source = HOME / 'releases' / sha
    require_plain_path(source)
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        log(f'Cloning verified main candidate {sha[:12]}')
        command(['git', '-c', 'core.longpaths=true', 'clone', '--depth', '1',
                 '--branch', 'main', f'https://github.com/{REPOSITORY}.git', str(source)],
                env=candidate_git_environment(), timeout=240, label='clone exact main')
    verify_candidate(source, sha)
    return source

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
    source = prepare_candidate(sha)
    if migration_digest(source) != migration_digest(old_source):
        raise RuntimeError('New migrations require offline guarded review; no automatic rollout')
    if ('MJOBS_PUBLIC_HOST' not in (source / 'compose.yml').read_text(encoding='utf-8')
            or 'MJOBS_IMAGE' not in (source / 'compose.yml').read_text(encoding='utf-8')):
        raise RuntimeError('Candidate lacks external-host deployment contract')
    for filename in ('.env', 'compose.micro.yml'):
        require_plain_path(BASE / filename)
        require_plain_path(source / filename)
        if not (BASE / filename).is_file():
            raise RuntimeError('Missing protected deployment overlay')
        shutil.copy2(BASE / filename, source / filename)
    image = 'mjobs-micro:sha-' + sha
    log('Building full tested image from verified source (no code agents)')
    command(['docker', 'build', '-f', 'infra/docker/Dockerfile', '-t', image, '.'],
            cwd=source, timeout=4500, label='validated Docker image build')
    verify_candidate(source, sha)
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
                cwd=old_source, env=env, timeout=900, label='quiescent backup', backup_diagnostics=True)
        command([str(OPS_PYTHON), '-m', 'cli.local_backup', 'verify',
                 '--archive', str(archive)], cwd=old_source, timeout=180,
                label='verify backup', backup_diagnostics=True)
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
    if os.name != 'nt':
        return 2
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
