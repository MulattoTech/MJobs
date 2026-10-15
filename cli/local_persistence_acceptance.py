"""Private-value-free observations for the existing synthetic connected harnesses.

This is an observer, not a backup implementation. PostgreSQL is observed through
the product API; no direct SQL or document filesystem shortcuts certify a domain.
The configuration digest observes the operator's existing local file. Restore
does not apply that file: matching it proves only retained manual configuration.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DOMAINS = ('postgresql', 'uploaded_resumes', 'generated_resumes', 'cover_letters',
           'application_history', 'company_source_catalog', 'configuration')
SOURCE_FIELDS = ('id', 'company_id', 'provider', 'tenant', 'endpoint', 'origin',
                 'enabled', 'priority', 'collection_strategy')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def documented_stop_services(path=ROOT / 'docs/runbooks/LOCAL_BACKUP.md'):
    """Parse data, never execute shell text. Require the complete documented set."""
    from cli.local_backup import WRITERS
    lines = re.findall(r'^docker compose -p \$project stop (.+)$',
                       Path(path).read_text(encoding='utf-8'), re.M)
    if len(lines) != 1:
        raise ValueError('Runbook must contain one writer stop command')
    services = tuple(lines[0].split())
    if len(services) != len(WRITERS) or set(services) != set(WRITERS):
        raise ValueError('Documented stop set must match all backup writers')
    return services


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'domains'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['domains'], dict) or set(value['domains']) != set(DOMAINS)):
        raise ValueError('Invalid seven-domain inventory schema')
    for name in DOMAINS:
        row = value['domains'][name]
        if (not isinstance(row, dict) or set(row) != {'count', 'sha256'}
                or type(row['count']) is not int or row['count'] < 1
                or not isinstance(row['sha256'], str)
                or re.fullmatch('[a-f0-9]{64}', row['sha256']) is None):
            raise ValueError('Invalid seven-domain inventory: ' + name)
    return value


def compare(before, after):
    validate(before)
    validate(after)
    for name in DOMAINS:
        if before['domains'][name] != after['domains'][name]:
            raise ValueError('Persistence inventory changed: ' + name)
    return {'result': 'PASS', 'domains': list(DOMAINS),
            'scope': 'Synthetic FIXTURE inventory only; PERSONAL runtime and complete startup command acceptance NOT_RUN',
            'configuration': 'Operator-managed local configuration retained; archive settings not applied'}


def compare_baseline(baseline, observed):
    if 'local_persistence' not in baseline:
        return {'result': 'NOT_RUN', 'reason': 'Legacy baseline has no seven-domain inventory'}
    return compare(baseline['local_persistence'], observed)


def collect(get_json, get_bytes, profile_id, application_id, *, root=ROOT):
    """Read the already-seeded synthetic fixture via API JSON and download seams.

    Callers retain their loopback, fixture and project admission gates. Only
    counts/digests escape this function, including metadata and control values.
    Empty domains and failed downloads are errors, never fabricated proof.
    """
    settings = get_json('/settings')
    if (settings.get('mode') != 'LOCAL_FIRST' or settings.get('operation_mode') != 'FIXTURE'
            or settings.get('external_submission_enabled') is not False):
        raise ValueError('Persistence proof requires synthetic LOCAL_FIRST FIXTURE mode')
    prefix = '/profiles/' + profile_id
    profile = get_json(prefix)
    application = get_json(prefix + '/applications/' + application_id)
    if (profile['id'] != profile_id or application['profile_id'] != profile_id
            or application['id'] != application_id or application['state'] != 'CONFIRMED'
            or application['target'] != 'MOCK' or not application['attempts']
            or application['confirmation']['submission_count'] != 1):
        raise ValueError('Persistence proof requires matching completed synthetic application history')
    values = {'postgresql': [profile]}
    uploads = get_json(prefix + '/resumes')['resumes']
    values['uploaded_resumes'] = []
    for row in sorted(uploads, key=lambda item: item['revision_id']):
        data = get_bytes(prefix + '/resumes/' + row['id'])
        if not data or hashlib.sha256(data).hexdigest() != row['sha256'] or len(data) != row['size_bytes']:
            raise ValueError('Uploaded resume bytes differ from stored metadata')
        values['uploaded_resumes'].append({'metadata': row, 'bytes_sha256': hashlib.sha256(data).hexdigest()})
    documents = get_json(prefix + '/applications/' + application_id + '/documents')['documents']
    for domain, kind in (('generated_resumes', 'resume'), ('cover_letters', 'cover-letter')):
        values[domain] = []
        for row in sorted(documents, key=lambda item: item['id']):
            if row['kind'] != kind:
                continue
            hashes = {}
            for format in ('txt', 'docx', 'pdf'):
                data = get_bytes(prefix + '/documents/' + row['id'] + '/' + format)
                hashes[format] = hashlib.sha256(data).hexdigest()
                if not data or hashes[format] != row['sha256'][format]:
                    raise ValueError('Generated document bytes differ from stored metadata')
            # Immutable producer metadata, excluding dynamic preview diagnostics.
            values[domain].append({'id': row['id'], 'version': row['version'],
                                   'profile_version': row['profile_version'], 'sha256': hashes})
    values['application_history'] = [{key: application[key] for key in
        ('id', 'profile_id', 'job_id', 'state', 'target', 'confirmation', 'attempts')}]
    sources = get_json('/company-sources')['company_sources']
    values['company_source_catalog'] = [
        {key: row[key] for key in SOURCE_FIELDS} for row in sorted(sources, key=lambda item: item['id'])]
    # Hash the actual local configuration, never ship its bytes or arbitrary keys.
    # The same operator-managed file is retained during fresh-target restoration.
    values['configuration'] = [{
        'env_sha256': hashlib.sha256((Path(root) / '.env').read_bytes()).hexdigest(),
        'compose_sha256': hashlib.sha256((Path(root) / 'compose.yml').read_bytes()).hexdigest(),
        'settings': {key: settings[key] for key in
                     ('mode', 'operation_mode', 'external_submission_enabled', 'worker_limits')},
    }]
    return validate({'schema_version': 1, 'domains': {
        name: {'count': len(values[name]), 'sha256': digest(values[name])} for name in DOMAINS}})


def observe_http(client, baseline):
    def response(path):
        result = client.get(path)
        result.raise_for_status()
        return result
    return collect(lambda path: response(path).json(), lambda path: response(path).content,
                   baseline['profile_id'], baseline['application_id'])


def require_running_backup_refusal(project, archive):
    from cli import local_backup
    try:
        local_backup.create_backup(project, archive)
    except local_backup.BackupError as error:
        if str(error) in {'Stop ' + service + ' before backing up the database and documents'
                          for service in local_backup.WRITERS}:
            return 'PASS'
        raise
    raise RuntimeError('Backup unexpectedly accepted running writers')


def require_corrupt_restore_refusal(archive, target, port):
    """Corrupt only an owned synthetic copy, retaining the source archive intact."""
    from cli import local_backup
    with tempfile.TemporaryDirectory(prefix='mjobs-corrupt-proof-') as directory:
        damaged = Path(directory) / 'archive'
        shutil.copytree(archive, damaged)
        with (damaged / 'database.dump').open('ab') as stream:
            stream.write(b'\nsynthetic-corruption-negative-control\n')
        try:
            local_backup.restore_backup(damaged, target, port)
        except local_backup.BackupError as error:
            if str(error) == 'Backup checksum mismatch: database.dump':
                return 'PASS'
            raise
    raise RuntimeError('Restore unexpectedly accepted a corrupt archive')


def require_occupied_restore_refusal(archive, target, port):
    from cli import local_backup
    try:
        local_backup.restore_backup(archive, target, port)
    except local_backup.BackupError as error:
        if str(error) == 'Target project or its data volumes already exists; choose a fresh project':
            return 'PASS'
        raise
    raise RuntimeError('Restore unexpectedly accepted an occupied target')
